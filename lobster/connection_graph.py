"""Keeping Octopus's connection graph and Lobster's navmesh from disagreeing
(Scope 10.4-10.5).

> If the destruction is severe enough to sever a `connections` edge entirely
> (the gatehouse *was* the only way through), that's a `patch_record` against
> the Location's `connections` field, going through Octopus's ordinary
> record-update path and triggering its normal re-resolution.
>
> These are two different systems (Lobster's navmesh, Octopus's connection
> graph) that both need to react to the same destruction event; a test should
> confirm the graph and the navmesh never disagree about whether a path exists.

Two things this module is careful about:

**The operation.** `Location.connections` is `UNION_TOMBSTONED`, and Octopus
rejects a `PATCH` that would wholesale-replace a union field. The operation that
removes one entry is `DELETE_ENTRY`, and entry identity for a tombstoned union
is canonical whole-value equality - so the entry is passed back **verbatim as
resolved**, never rebuilt field by field. Reconstructing it would produce a
value that does not match, and the delete would silently do nothing.
See DECISIONS.md D4.

**Both directions.** A collapsed gatehouse is not a one-way passage; it is no
passage. Severing only the outbound edge would leave a connection Octopus's own
lint reports as one-way (`lce.lint`, D26) and would let an NPC walk back
through the rubble from the other side. Both entries go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .geometry import Vec3
from .navmesh import Navmesh

CONNECTIONS_FIELD = "connections"


class ConnectionGraphError(Exception):
    pass


@dataclass(frozen=True)
class Severance:
    """One connection Lobster believes is no longer walkable, and the ops that
    tell Octopus so."""

    location_id: str
    target_location_id: str
    reason: str
    ops: Tuple[Dict[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"location_id": self.location_id,
                "target_location_id": self.target_location_id,
                "reason": self.reason, "ops": [dict(o) for o in self.ops]}


def connection_entry(location_record: Dict[str, Any],
                     target_location_id: str) -> Optional[Dict[str, Any]]:
    """The resolved entry, verbatim. Never reconstructed (D4)."""
    for conn in location_record.get(CONNECTIONS_FIELD) or ():
        if isinstance(conn, dict) and conn.get("target_location_id") == target_location_id:
            return conn
        if isinstance(conn, str) and conn == target_location_id:
            return conn
    return None


def delete_entry_op(location_id: str, entry: Any) -> Dict[str, Any]:
    return {"op": "DELETE_ENTRY", "id": location_id,
            "field": CONNECTIONS_FIELD, "value": entry}


def sever_ops(view: Any, location_id: str,
              target_location_id: str) -> List[Dict[str, Any]]:
    """DELETE_ENTRY for both halves of a connection, if they resolve."""
    ops: List[Dict[str, Any]] = []
    for a, b in ((location_id, target_location_id),
                 (target_location_id, location_id)):
        record = view.record(a)
        if record is None:
            continue
        entry = connection_entry(record, b)
        if entry is not None:
            ops.append(delete_entry_op(a, entry))
    return ops


def portal_polys(navmesh: Navmesh) -> Dict[str, List[int]]:
    """Which navmesh polys are the doors out of this cell, by target cell."""
    out: Dict[str, List[int]] = {}
    for pid, poly in navmesh.polys.items():
        if poly.connection_target:
            out.setdefault(poly.connection_target, []).append(pid)
    for target in out:
        out[target].sort()
    return out


def navmesh_says_connected(navmesh: Navmesh, reference_poly: int,
                           target_location_id: str) -> Optional[bool]:
    """Can an agent still walk from the cell's reference poly to that door?

    Returns None when this cell's navmesh declares no portal polygon for that
    target - a door Lobster's geometry does not model. **None is not False.**
    A cell whose navmesh says nothing about a connection has no geometric
    opinion about it, and severing on no opinion would delete authored
    connections for the crime of not being baked yet. Lobster reports what it
    measured (L4); it does not fill in silence with a verdict.

    `avoid_dirty=False` on purpose: this asks about *geometry*, and it is only
    ever called after the patch that made the geometry current. Asking it while
    polys are still pending would conflate "not reachable" with "not yet
    recomputed", and severing on the second would be a permanent record write
    caused by a temporary state.
    """
    portals = portal_polys(navmesh).get(target_location_id)
    if not portals:
        return None
    return any(navmesh.reachable(reference_poly, pid, avoid_dirty=False)
               for pid in portals)


class ConnectionGraphPatcher:
    """Re-checks a cell's connections after a navmesh patch and severs what is
    genuinely gone.

    Runs *after* the async patch completes, never during it, and never
    speculatively: a connection is severed only when the navmesh, fully
    recomputed, says the door cannot be reached.
    """

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.severed: List[Severance] = []

    def _write(self, op: Dict[str, Any]) -> Dict[str, Any]:
        if self.session is None:
            return op
        engine = getattr(self.session, "engine", None)
        if engine is not None and hasattr(engine, "write"):
            return engine.write(op)
        return self.session.save.append(op)

    def check_cell(self, view: Any, cell_id: str, navmesh: Navmesh,
                   reference_poly: int, *, apply: bool = True) -> List[Severance]:
        """Every connection out of this cell, checked against the navmesh."""
        record = view.record(cell_id)
        if record is None:
            return []
        out: List[Severance] = []
        for conn in record.get(CONNECTIONS_FIELD) or ():
            target = (conn.get("target_location_id") if isinstance(conn, dict)
                      else conn)
            if not target:
                continue
            verdict = navmesh_says_connected(navmesh, reference_poly, target)
            if verdict is not False:
                continue    # reachable, or no portal baked -> no opinion
            ops = sever_ops(view, cell_id, target)
            if not ops:
                continue
            severance = Severance(
                location_id=cell_id, target_location_id=target,
                reason="no walkable navmesh route from poly {0} to any portal "
                       "into {1!r} after a load-bearing destruction".format(
                           reference_poly, target),
                ops=tuple(ops))
            if apply:
                for op in ops:
                    self._write(op)
            self.severed.append(severance)
            out.append(severance)
        return out

    def report(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.severed]


def graph_says_connected(view: Any, from_id: str, to_id: str) -> bool:
    """Octopus's own answer, via `lce.graph.shortest_path`.

    The agreement test compares this against `navmesh_says_connected`; using
    Octopus's real traversal rather than a reimplementation is the point.
    """
    return view.connection_path(from_id, to_id) is not None
