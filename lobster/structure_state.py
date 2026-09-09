"""Break-state as an Octopus record (Scope 6, L5).

L5: "Damage that must be remembered is an Octopus record, not a Lobster save
file."

There is exactly one record type on this path -
`StructureState{id, location_id, destroyed_chunks: Set<u16>}` with
`UNION_TOMBSTONED` - and exactly one way to write it: an ordinary layer
operation through the session the game already has. No private channel, no
side file, no "just for structures" shortcut. `writer.py` is deliberately thin
for that reason: if it ever grows a persistence path of its own, that is the
bug.

Which operation for which act:

* **Destruction** is `MERGE(id, "destroyed_chunks", [indices])`. Additive and
  monotonic, which is why Scope 6 chose `UNION_TOMBSTONED` over
  `REPLACE_PER_SUBFIELD`: "two mods damaging different walls of the same keep
  should both apply."
* **Repair** is `DELETE_ENTRY(id, "destroyed_chunks", index)`, one per index -
  "the rare explicit act, modeled as its own tombstone reversal, not a second
  merge semantics." Octopus folds a union field in global op order, so a later
  MERGE of the same index re-destroys it correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Set,
                    Tuple)

from .events import EventBus
from .octopus_bridge import STRUCTURE_STATE_TYPE

DESTROYED_CHUNKS_FIELD = "destroyed_chunks"


class StructureStateError(Exception):
    pass


# ---------------------------------------------------------------------------
# Operations as plain data (Octopus SDS 7.1: "a manifest IS a serialized
# operation log"). These are pure functions - callable in a test with no
# session, dumpable by the CLI, diffable by a reviewer.
# ---------------------------------------------------------------------------

def create_op(structure_id: str, location_id: str,
              destroyed_chunks: Optional[Sequence[int]] = None,
              display_name: Optional[str] = None) -> Dict[str, Any]:
    """The CREATE a content package emits for an authored structure.

    `lobster-build` writes one of these per structure so that a save's later
    MERGE has a target record. It is content, not save data: the structure
    exists because a package says so, and it stops existing when that package
    is removed - which is precisely what makes the quarantine round trip in
    section 14 test 2 work.
    """
    record: Dict[str, Any] = {
        "id": structure_id,
        "type": STRUCTURE_STATE_TYPE,
        "location_id": location_id,
        DESTROYED_CHUNKS_FIELD: sorted({int(i) for i in (destroyed_chunks or ())}),
    }
    if display_name:
        record["display_name"] = display_name
    return {"op": "CREATE", "record": record}


def destroy_op(structure_id: str,
               chunk_indices: Iterable[int]) -> Optional[Dict[str, Any]]:
    """MERGE the destroyed chunk indices. None if there is nothing to write."""
    values = sorted({int(i) for i in chunk_indices})
    if not values:
        return None
    return {"op": "MERGE", "id": structure_id,
            "field": DESTROYED_CHUNKS_FIELD, "values": values}


def repair_ops(structure_id: str,
               chunk_indices: Iterable[int]) -> List[Dict[str, Any]]:
    """DELETE_ENTRY per repaired chunk index."""
    return [{"op": "DELETE_ENTRY", "id": structure_id,
             "field": DESTROYED_CHUNKS_FIELD, "value": int(i)}
            for i in sorted({int(v) for v in chunk_indices})]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchDamage:
    """What a batched destruction actually did.

    `skipped` is not a failure - it is the chunks that were already gone, which
    a repeated offscreen siege produces constantly. Reporting them separately
    lets a caller tell "nothing left to knock down" from "nothing happened".
    """

    written: Dict[str, Tuple[int, ...]] = dc_field(default_factory=dict)
    skipped: Dict[str, Tuple[int, ...]] = dc_field(default_factory=dict)
    ops: Tuple[Dict[str, Any], ...] = ()

    def structures_damaged(self) -> int:
        return len(self.written)

    def chunks_destroyed(self) -> int:
        return sum(len(v) for v in self.written.values())

    def to_dict(self) -> Dict[str, Any]:
        return {"written": {k: list(v) for k, v in sorted(self.written.items())},
                "skipped": {k: list(v) for k, v in sorted(self.skipped.items())},
                "ops": [dict(o) for o in self.ops],
                "structures_damaged": self.structures_damaged(),
                "chunks_destroyed": self.chunks_destroyed()}


class StructureStateWriter:
    """Applies structure damage as Octopus operations, and reports it.

    Two responsibilities, in this order:

    1. write the op through Octopus (so it is remembered), and
    2. fire `on_structure_damaged(structure_id, chunk_indices[])` (so somebody
       else can decide what it means).

    It never interprets. Nothing here knows what a destroyed gatehouse costs, or
    whether the keep has fallen; that is content's call (L4).
    """

    def __init__(self, session: Any, bus: Optional[EventBus] = None) -> None:
        self.session = session
        self.bus = bus
        self.written: List[Dict[str, Any]] = []

    def _write(self, op: Dict[str, Any]) -> Dict[str, Any]:
        engine = getattr(self.session, "engine", None)
        if engine is not None and hasattr(engine, "write"):
            tagged = engine.write(op)
        else:
            tagged = self.session.save.append(op)
        self.written.append(tagged)
        return tagged

    def destroy(self, structure_id: str,
                chunk_indices: Iterable[int]) -> List[int]:
        """Remember destruction, then report it. Returns what was written."""
        op = destroy_op(structure_id, chunk_indices)
        if op is None:
            return []
        self._write(op)
        if self.bus is not None:
            self.bus.structure_damaged(structure_id, op["values"])
        return list(op["values"])

    def destroy_many(self, damage: Iterable[Tuple[str, Iterable[int]]], *,
                     skip_redundant: bool = True) -> BatchDamage:
        """Destroy chunks across many structures in one pass.

        Built for the case Shrimp's siege resolver has: a town that is not
        resident gets a computed outcome, and every building the raid overran
        needs its `destroyed_chunks` updated at once. It needs no resident cell -
        the writer only ever holds a session.

        **This is not primarily a speed fix, and it is worth saying so.** A
        single `destroy` costs about 7 microseconds; two hundred of them cost
        1.5 ms, because Octopus's resolution is lazy and incremental - the
        writes queue and one re-resolution settles them all. Looping `destroy`
        was already cheap. What it was *not* is safe:

        **1. Atomic.** Every entry is validated before any op is written. A
        siege listing one structure that does not resolve used to fail midway,
        leaving the town half-sacked with some ops committed, some events fired,
        and no way back. Now it raises having written nothing.

        **2. Free of redundant writes.** Ten re-runs of the same siege used to
        append fifty operations, forty-five of which were no-ops that
        `UNION_TOMBSTONED` folded away at resolve time. The resolved world was
        right and the save file grew forever. An offscreen resolver runs for
        game-years, so that is unbounded growth traceable to the shell rather
        than to content, which L7 forbids. One resolution read for the whole
        batch now skips chunks that are already gone.

        **3. Coalesced.** Two entries naming the same structure are one
        structure taking damage: they merge into one operation and one Event.

        `skip_redundant=False` writes unconditionally, which is what a caller
        that has already diffed against the world wants.

        Events are still one `on_structure_damaged` per structure. Scope 13
        fixes that shape and there is no batch Event - a consumer that wants one
        notification per siege can count them itself.

        Validation covers what a writer can see: the structure resolves, and the
        indices are non-negative integers. It deliberately does **not** check
        them against `grid_size` - that needs the authored bundle, and Scope 6
        already handles a stale index at load time by quarantining it with a
        reason rather than crashing the mesher.
        """
        # ---- normalise and coalesce ---------------------------------------
        wanted: Dict[str, Set[int]] = {}
        order: List[str] = []
        for structure_id, chunk_indices in damage:
            if structure_id not in wanted:
                wanted[structure_id] = set()
                order.append(structure_id)
            for raw in chunk_indices:
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise StructureStateError(
                        "structure {0!r}: chunk index {1!r} is not an "
                        "integer".format(structure_id, raw))
                if raw < 0:
                    raise StructureStateError(
                        "structure {0!r}: chunk index {1} is "
                        "negative".format(structure_id, raw))
                wanted[structure_id].add(int(raw))

        # ---- validate everything before writing anything ------------------
        resolution = None
        if skip_redundant or wanted:
            resolution = self.session.resolution()
        for structure_id in order:
            record = resolution.get(structure_id)
            if record is None:
                raise StructureStateError(
                    "structure {0!r} does not resolve to a record, so its "
                    "damage could never be remembered (L5). Nothing has been "
                    "written - the batch of {1} structure(s) was rejected "
                    "whole.".format(structure_id, len(order)))
            if record.get("type") != STRUCTURE_STATE_TYPE:
                raise StructureStateError(
                    "{0!r} is a {1}, not a {2}. Nothing has been "
                    "written.".format(structure_id, record.get("type"),
                                      STRUCTURE_STATE_TYPE))

        # ---- build the operations -----------------------------------------
        result_written: Dict[str, Tuple[int, ...]] = {}
        result_skipped: Dict[str, Tuple[int, ...]] = {}
        pending: List[Tuple[str, Dict[str, Any]]] = []
        for structure_id in order:
            already: Set[int] = set()
            if skip_redundant:
                record = resolution.get(structure_id) or {}
                already = {int(i) for i in record.get(DESTROYED_CHUNKS_FIELD) or ()
                           if isinstance(i, int) and not isinstance(i, bool)}
            fresh = wanted[structure_id] - already
            redundant = wanted[structure_id] & already
            if redundant:
                result_skipped[structure_id] = tuple(sorted(redundant))
            op = destroy_op(structure_id, fresh)
            if op is None:
                continue
            pending.append((structure_id, op))
            result_written[structure_id] = tuple(op["values"])

        # ---- commit --------------------------------------------------------
        for structure_id, op in pending:
            self._write(op)
        if self.bus is not None:
            for structure_id, op in pending:
                self.bus.structure_damaged(structure_id, op["values"])

        return BatchDamage(written=result_written, skipped=result_skipped,
                           ops=tuple(op for _sid, op in pending))

    def repair(self, structure_id: str,
               chunk_indices: Iterable[int]) -> List[int]:
        ops = repair_ops(structure_id, chunk_indices)
        for op in ops:
            self._write(op)
        return [op["value"] for op in ops]

    def apply_live(self, live_structure: Any,
                   chunk_indices: Iterable[int]) -> List[int]:
        """Damage a resident structure: geometry first, then the record.

        Geometry first is not an ordering preference, it is Scope 10's rule:

        > the physical voxel collider updates **synchronously**, on the exact
        > frame of impact - a player or NPC standing in the rubble the instant
        > it forms must not fall through or clip.

        The record write and the Event that follows are allowed to be the
        slower half; the collider never waits on them.
        """
        newly = live_structure.destroy_chunks(chunk_indices)
        if not newly:
            return []
        self.destroy(live_structure.structure_id, newly)
        return newly


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def structure_state_record(view: Any, structure_id: str) -> Optional[Dict[str, Any]]:
    """The live `StructureState`, read through the frame view."""
    rec = view.record(structure_id)
    if rec is None:
        return None
    if rec.get("type") != STRUCTURE_STATE_TYPE:
        raise StructureStateError(
            "{0!r} resolves to a {1}, not a {2}".format(
                structure_id, rec.get("type"), STRUCTURE_STATE_TYPE))
    return rec


def quarantine_report(resolutions: Iterable[Any]) -> List[Dict[str, Any]]:
    """Every stale `destroyed_chunks` index found across a cell load.

    Section 14 test 9 asserts on this: loading a `StructureState` whose indices
    no longer fit the current `grid_size` quarantines only the stale indices,
    logs why, and never crashes the mesh builder or discards the record.
    """
    out: List[Dict[str, Any]] = []
    for res in resolutions:
        out.extend(dict(q) for q in getattr(res, "quarantined", ()))
    return out
