"""Declared, enforced, attributable budgets (L6, Scope 13 invariant 2, 14).

L6: "Budgets are declared and enforced per cell, not assumed for the whole
world."

Section 13: "Failure is visible and attributable - over-budget content names
the specific record, the specific cell."

Two objects live here:

``Budget``
    The declared ceiling for one cell, read off the Location record's
    ``lobster_budget`` field, falling back to the constants in
    ``lobster.constants``. A cell may declare a LOWER ceiling than the default;
    declaring a HIGHER one is itself a violation, so a cell cannot quietly buy
    itself more room than the shell promises.

``MemoryLedger``
    What is actually resident right now, per cell, per pool. The pools are
    named and disjoint; L2's terrain/structure separation is a property of this
    object as much as of the meshers, and ``assert_pools_disjoint`` is the
    machine-checkable half of it.

Neither object decides anything. They measure, and they raise with the record
id and the cell id in the message.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Set

from .constants import (
    DEFAULT_MAX_ACTIVE_SKELETONS_PER_CELL, DEFAULT_MAX_CELL_BYTES,
    DEFAULT_MAX_MICRO_CHUNKS_PER_CELL, DEFAULT_MAX_STRUCTURE_VOXELS_PER_CELL,
    MAX_RESIDENT_CELLS, MAX_TRANSITION_PEAK_BYTES,
)

#: Pool names. Terrain and structures are separate entries because L2 says they
#: are separate systems; the separation test reads these names.
POOL_TERRAIN = "terrain"
POOL_STRUCTURES = "structures"
POOL_NAVMESH = "navmesh"
POOL_LIGHTMAP = "lightmap"
POOL_METADATA = "metadata"

ALL_POOLS = (POOL_TERRAIN, POOL_STRUCTURES, POOL_NAVMESH, POOL_LIGHTMAP,
             POOL_METADATA)


class BudgetViolation(Exception):
    """Over-budget content. The message names the cell and the record.

    Deliberately an exception and not a warning. Section 15.5: "Cell transition
    is a budget moment, not a loading screen." Content that does not fit is a
    content bug, and a shell that absorbs it silently is how bloat stops being
    traceable to content (L7).
    """

    def __init__(self, *, cell_id: str, record_id: Optional[str], metric: str,
                 value: Any, limit: Any, detail: str = "") -> None:
        self.cell_id = cell_id
        self.record_id = record_id
        self.metric = metric
        self.value = value
        self.limit = limit
        self.detail = detail
        who = "record {0!r}".format(record_id) if record_id else "cell contents"
        msg = ("cell {0!r}: {1} exceeds declared budget {2!r}: {3} > {4}"
               .format(cell_id, who, metric, value, limit))
        if detail:
            msg += " ({0})".format(detail)
        super().__init__(msg)

    def to_dict(self) -> Dict[str, Any]:
        return {"cell_id": self.cell_id, "record_id": self.record_id,
                "metric": self.metric, "value": self.value,
                "limit": self.limit, "detail": self.detail}


_BUDGET_FIELDS = (
    ("max_structure_voxels", DEFAULT_MAX_STRUCTURE_VOXELS_PER_CELL),
    ("max_micro_chunks", DEFAULT_MAX_MICRO_CHUNKS_PER_CELL),
    ("max_active_skeletons", DEFAULT_MAX_ACTIVE_SKELETONS_PER_CELL),
    ("max_bytes", DEFAULT_MAX_CELL_BYTES),
)


@dataclass(frozen=True)
class Budget:
    """One cell's declared ceilings."""

    cell_id: str
    max_structure_voxels: int = DEFAULT_MAX_STRUCTURE_VOXELS_PER_CELL
    max_micro_chunks: int = DEFAULT_MAX_MICRO_CHUNKS_PER_CELL
    max_active_skeletons: int = DEFAULT_MAX_ACTIVE_SKELETONS_PER_CELL
    max_bytes: int = DEFAULT_MAX_CELL_BYTES

    @classmethod
    def declared(cls, cell_id: str,
                 location_record: Optional[Dict[str, Any]] = None) -> "Budget":
        """Read a cell's declared budget off its Location record.

        A cell that declares nothing gets the shell defaults. A cell that
        declares MORE than the shell default is rejected here, naming itself -
        the alternative is a per-cell escape hatch that makes the global
        promise meaningless.
        """
        raw = (location_record or {}).get("lobster_budget") or {}
        if not isinstance(raw, dict):
            raise BudgetViolation(
                cell_id=cell_id, record_id=cell_id, metric="lobster_budget",
                value=type(raw).__name__, limit="object",
                detail="lobster_budget must be an object of declared ceilings")
        kwargs: Dict[str, int] = {}
        for name, default in _BUDGET_FIELDS:
            if name not in raw:
                continue
            value = raw[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BudgetViolation(
                    cell_id=cell_id, record_id=cell_id, metric=name,
                    value=value, limit=default,
                    detail="declared ceiling must be a non-negative integer")
            if value > default:
                raise BudgetViolation(
                    cell_id=cell_id, record_id=cell_id, metric=name,
                    value=value, limit=default,
                    detail="a cell may declare a lower ceiling than the shell "
                           "default, never a higher one")
            kwargs[name] = value
        return cls(cell_id=cell_id, **kwargs)

    def limit_for(self, metric: str) -> int:
        return int(getattr(self, metric))

    def check(self, metric: str, value: int, *,
              record_id: Optional[str] = None, detail: str = "") -> None:
        limit = self.limit_for(metric)
        if value > limit:
            raise BudgetViolation(cell_id=self.cell_id, record_id=record_id,
                                  metric=metric, value=value, limit=limit,
                                  detail=detail)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"cell_id": self.cell_id}
        for name, _ in _BUDGET_FIELDS:
            out[name] = getattr(self, name)
        return out


def _empty_pools() -> Dict[str, int]:
    return {p: 0 for p in ALL_POOLS}


def _empty_pool_objects() -> Dict[str, Set[int]]:
    return {p: set() for p in ALL_POOLS}


@dataclass
class CellCharge:
    """What one resident cell is currently costing, per pool."""

    cell_id: str
    pools: Dict[str, int] = dc_field(default_factory=_empty_pools)
    #: object ids per pool - the machine-checkable half of L2.
    pool_object_ids: Dict[str, Set[int]] = dc_field(
        default_factory=_empty_pool_objects)

    def total(self) -> int:
        return sum(self.pools.values())


class MemoryLedger:
    """Accounted residency across all loaded cells.

    Accounted, not sampled: the ledger is charged explicitly by the loader with
    the byte cost each artifact declares. That makes the section 14 test 4 peak
    assertion deterministic and CI-safe rather than an RSS reading that varies
    with the allocator - which is what "asserted in CI, not eyeballed" requires.
    """

    def __init__(self, *, peak_limit: int = MAX_TRANSITION_PEAK_BYTES,
                 max_resident_cells: int = MAX_RESIDENT_CELLS) -> None:
        self.peak_limit = peak_limit
        self.max_resident_cells = max_resident_cells
        self.cells: Dict[str, CellCharge] = {}
        self.peak_bytes = 0
        self.peak_cells = 0
        self.history: List[Dict[str, Any]] = []

    # -- charging ------------------------------------------------------------
    def charge(self, cell_id: str, pool: str, nbytes: int, *,
               obj: Any = None, budget: Optional[Budget] = None,
               record_id: Optional[str] = None) -> None:
        if pool not in ALL_POOLS:
            raise ValueError("unknown memory pool {0!r}".format(pool))
        charge = self.cells.setdefault(cell_id, CellCharge(cell_id=cell_id))
        charge.pools[pool] += int(nbytes)
        if obj is not None:
            charge.pool_object_ids[pool].add(id(obj))
        if budget is not None:
            budget.check("max_bytes", charge.total(), record_id=record_id,
                         detail="pool {0!r}".format(pool))
        self._observe("charge", cell_id)

    def release(self, cell_id: str) -> None:
        self.cells.pop(cell_id, None)
        self._observe("release", cell_id)

    def _observe(self, action: str, cell_id: str) -> None:
        total = self.total_bytes()
        resident = len(self.cells)
        self.peak_bytes = max(self.peak_bytes, total)
        self.peak_cells = max(self.peak_cells, resident)
        self.history.append({"action": action, "cell_id": cell_id,
                             "total_bytes": total,
                             "resident_cells": resident})
        if total > self.peak_limit:
            raise BudgetViolation(
                cell_id=cell_id, record_id=None,
                metric="peak_transition_bytes", value=total,
                limit=self.peak_limit,
                detail="{0} cells resident during {1}".format(resident, action))
        if resident > self.max_resident_cells:
            raise BudgetViolation(
                cell_id=cell_id, record_id=None, metric="max_resident_cells",
                value=resident, limit=self.max_resident_cells,
                detail="during {0}".format(action))

    # -- reading -------------------------------------------------------------
    def total_bytes(self) -> int:
        return sum(c.total() for c in self.cells.values())

    def pool_bytes(self, pool: str) -> int:
        return sum(c.pools.get(pool, 0) for c in self.cells.values())

    def objects_in_pool(self, pool: str) -> Set[int]:
        out: Set[int] = set()
        for c in self.cells.values():
            out |= c.pool_object_ids.get(pool, set())
        return out

    def assert_pools_disjoint(self, *pools: str) -> None:
        """L2, mechanised: terrain and structures are never the same system.

        Two pools sharing an object id means one allocation is being counted -
        and therefore owned - by both systems. That is the exact failure L2
        exists to prevent, so it raises rather than warns.
        """
        seen: Dict[int, str] = {}
        for pool in (pools or (POOL_TERRAIN, POOL_STRUCTURES)):
            for oid in self.objects_in_pool(pool):
                if oid in seen and seen[oid] != pool:
                    raise BudgetViolation(
                        cell_id="<ledger>", record_id=None,
                        metric="pool_separation",
                        value="{0}+{1}".format(seen[oid], pool),
                        limit="disjoint",
                        detail="object {0} is charged to both pools "
                               "(L2)".format(oid))
                seen[oid] = pool

    def report(self) -> Dict[str, Any]:
        return {
            "resident_cells": sorted(self.cells),
            "total_bytes": self.total_bytes(),
            "peak_bytes": self.peak_bytes,
            "peak_limit": self.peak_limit,
            "peak_resident_cells": self.peak_cells,
            "max_resident_cells": self.max_resident_cells,
            "by_pool": {p: self.pool_bytes(p) for p in ALL_POOLS},
            "by_cell": {cid: {"total": c.total(), "pools": dict(c.pools)}
                        for cid, c in sorted(self.cells.items())},
        }


def declared_budgets(
        location_records: Iterable[Dict[str, Any]]) -> Dict[str, Budget]:
    """Every Location's declared budget, for the CLI's budget dump."""
    return {rec["id"]: Budget.declared(rec["id"], rec)
            for rec in location_records}


# ---------------------------------------------------------------------------
# Composition: does a cell's declared budget leave room for its ring?
# ---------------------------------------------------------------------------

def transition_peak_findings(
        view: Any, *, peak_limit: int = MAX_TRANSITION_PEAK_BYTES,
        actual_bytes: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Cells whose declared budgets cannot compose across a transition.

    A per-cell ceiling is only meaningful next to the residency it will actually
    be part of. `DEFAULT_MAX_CELL_BYTES` x `MAX_RESIDENT_CELLS` used to come to
    more than twice `MAX_TRANSITION_PEAK_BYTES`, so a cell that declared nothing
    got a ceiling it could never be allowed to use, and an exterior cell in any
    ring layout was *obliged* to declare lower - an obligation nothing stated.
    The failure surfaced as a `BudgetViolation` at a transition, far from the
    manifest that caused it.

    This is that check moved to where a writer can see it. A transition holds
    the union of both residency sets (load-then-unload, Scope 15.5), so for each
    cell it walks every transition out of it and sums the declared ceilings over
    that union.

    `actual_bytes` - the real baked cost per cell - turns the same walk into a
    prediction rather than a promise, and is reported alongside. A cell may
    declare 48 MiB and use 3; only the second number says whether a transition
    will really fail.

    Reports; never raises. The caller decides whether it fails a build.
    """
    # Imported here rather than at module scope: `lobster.cell` imports this
    # module, and the residency rule has to live next to the loader that uses it
    # so the two cannot drift.
    from .cell import residency_ring

    locations = view.records_of_type("Location")
    budgets = {rec["id"]: Budget.declared(rec["id"], rec) for rec in locations}
    rings = {rec["id"]: residency_ring(view, rec["id"]) for rec in locations}
    actual = actual_bytes or {}

    findings: List[Dict[str, Any]] = []
    for record in locations:
        cell_id = record["id"]
        worst: Optional[Dict[str, Any]] = None
        neighbours = [t for t, _cost in view.connections(cell_id)]
        for other in [cell_id] + neighbours:
            union = set(rings.get(cell_id, [cell_id]))
            union |= set(rings.get(other, [other]))
            declared = sum(budgets[c].max_bytes for c in union if c in budgets)
            if worst is None or declared > worst["declared_bytes"]:
                worst = {
                    "into": other,
                    "resident": sorted(union),
                    "declared_bytes": declared,
                    "actual_bytes": sum(actual.get(c, 0) for c in union),
                }
        if worst is None or worst["declared_bytes"] <= peak_limit:
            continue
        occasion = ("{0!r} resident by itself".format(cell_id)
                    if worst["into"] == cell_id
                    else "entering {0!r} from {1!r}".format(cell_id,
                                                            worst["into"]))
        findings.append({
            "code": "over_transition_peak",
            "record_id": cell_id,
            "cell_id": cell_id,
            "detail": "{0} holds {1} cells resident ({2}), whose declared "
                      "max_bytes total {3} against a {4} transition peak. A "
                      "cell's declared budget has to leave room for its ring, "
                      "not just for itself - lower these cells' "
                      "lobster_budget.max_bytes. Actual baked cost of that "
                      "union is {5}.".format(
                          occasion, len(worst["resident"]),
                          ", ".join(worst["resident"]),
                          worst["declared_bytes"], peak_limit,
                          worst["actual_bytes"] or "unknown"),
            "resident": worst["resident"],
            "declared_bytes": worst["declared_bytes"],
            "actual_bytes": worst["actual_bytes"],
            "limit": peak_limit,
        })
    return findings
