"""Baked navmesh, and the bounded escape hatch for destructible geometry
(Scope 10).

The common case is solved by not solving it: terrain is static, so its navmesh
is baked per cell and never regenerated. Everything in this module exists for
the rare case where destroying a structure genuinely changes what is reachable.

Three rules from Scope 10 shape the code:

**1. Load-bearing is inferred, not authored.** (v0.4)

> `lobster-build` bakes the cell's navmesh as usual, then, for every
> micro-chunk in every structure in that cell, tests whether the chunk's
> bounding volume intersects any navmesh polygon. If it does, the chunk is
> automatically flagged `navmesh_load_bearing: true` [...] Authors may still
> **override** the inferred flag on a specific chunk.

Scope 15.7 makes going back to an author-set flag a *regression*, so the
inference lives in the build step (`lobster.build.navmesh_inference`) and the
runtime only ever reads what it produced.

**2. Recompute, do not guess a direction.** A destroyed chunk can make a poly
*more* walkable (a wall comes down) or *less* (the deck under it does).
`recompute_polys` re-derives walkability from the geometry that is actually
there now - support below, clearance above - rather than applying a delta rule
that would be right half the time.

**3. Collision is synchronous, pathing may lag.** (v0.4)

> the physical voxel collider updates **synchronously**, on the exact frame of
> impact [...] The navmesh patch and any connection-graph patch are allowed to
> **defer asynchronously**. While a cell's navmesh node is marked
> dirty/pending, any AI whose current path or leash target passes through that
> node **holds position**.

Scope 15.11: any shortcut that makes the collider wait on the navmesh (or vice
versa) reintroduces the race. `PatchQueue` therefore has no way to block, and
nothing in `lobster.structures` can reach it.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field as dc_field
from typing import (Any, Dict, FrozenSet, Iterable, List, Optional, Sequence,
                    Set, Tuple)

from .geometry import (AABB, Vec3, distance, polygon_contains_2d)

#: Vertical space an agent needs above a walkable polygon.
DEFAULT_AGENT_HEIGHT_M = 1.8

#: How far below a polygon we look for something to stand on.
DEFAULT_SUPPORT_PROBE_M = 0.35

#: How far below a polygon `bounds()` reaches, and therefore how far below one
#: the build step's load-bearing inference can see (Scope 10).
#:
#: **This must never fall below `DEFAULT_SUPPORT_PROBE_M`**, and the two were
#: previously coupled by nothing but the arithmetic happening to work out. A
#: chunk sitting between the two depths would support a polygon - so destroying
#: it changes walkability - while intersecting no polygon bound, so the
#: inference would not flag it and no recompute would be queued. That is a
#: silent false negative, which is the exact failure mode Scope 15.7 says must
#: not come back:
#:
#: > it reopens the exact silent-failure mode (a forgotten flag on a gatehouse,
#: > NPCs walking through rubble with no error) that v0.4 exists to close.
#:
#: The margin is larger than the probe on purpose: the inference must err
#: towards over-flagging, which costs one recompute that changes nothing.
#: `tests/test_navmesh_agreement.py` pins the relationship so a future tweak to
#: either number fails loudly instead of opening the gap.
LOAD_BEARING_PROBE_MARGIN_M = 0.5


class NavmeshError(Exception):
    pass


@dataclass(frozen=True)
class NavPoly:
    """One convex walkable polygon, expressed on the XZ plane at height `y`.

    Convex and planar by construction from the build step: it keeps
    point-in-poly cheap and makes "walk between adjacent polys" a straight
    line, which is all the leash movement in Scope 9 needs.
    """

    poly_id: int
    points: Tuple[Tuple[float, float], ...]
    y: float
    neighbours: Tuple[int, ...] = ()
    #: portal polys carry the connection they belong to (Scope 4: "the
    #: connection owns the spawn point").
    connection_target: Optional[str] = None

    def contains(self, point: Vec3) -> bool:
        return polygon_contains_2d(self.points, point[0], point[2])

    def center(self) -> Vec3:
        n = len(self.points)
        return (sum(p[0] for p in self.points) / n, self.y,
                sum(p[1] for p in self.points) / n)

    def bounds(self) -> AABB:
        xs = [p[0] for p in self.points]
        zs = [p[1] for p in self.points]
        return AABB((min(xs), self.y - LOAD_BEARING_PROBE_MARGIN_M, min(zs)),
                    (max(xs), self.y + DEFAULT_AGENT_HEIGHT_M, max(zs)))

    def to_dict(self) -> Dict[str, Any]:
        return {"poly_id": self.poly_id,
                "points": [list(p) for p in self.points],
                "y": self.y, "neighbours": list(self.neighbours),
                "connection_target": self.connection_target}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "NavPoly":
        return cls(poly_id=int(raw["poly_id"]),
                   points=tuple((float(p[0]), float(p[1]))
                                for p in raw["points"]),
                   y=float(raw.get("y", 0.0)),
                   neighbours=tuple(int(n) for n in raw.get("neighbours", ())),
                   connection_target=raw.get("connection_target"))


class Navmesh:
    """A cell's baked navmesh plus its live blocked/dirty state.

    `blocked` and `dirty` are runtime state, never baked and never saved: they
    are derived from `StructureState` on load, exactly like the mesh is.
    """

    def __init__(self, cell_id: str, polys: Sequence[NavPoly]) -> None:
        self.cell_id = cell_id
        self.polys: Dict[int, NavPoly] = {p.poly_id: p for p in polys}
        self.blocked: Set[int] = set()
        self.dirty: Set[int] = set()
        #: bumped whenever a patch completes, so a consumer can tell whether
        #: the path it holds predates the last change.
        self.version = 0

    # -- reading -------------------------------------------------------------
    def poly(self, poly_id: int) -> NavPoly:
        p = self.polys.get(poly_id)
        if p is None:
            raise NavmeshError("{0}: no navmesh polygon {1}".format(
                self.cell_id, poly_id))
        return p

    def poly_at(self, point: Vec3) -> Optional[int]:
        best: Optional[int] = None
        best_dy = None
        for pid, poly in self.polys.items():
            if not poly.contains(point):
                continue
            dy = abs(poly.y - point[1])
            if best_dy is None or dy < best_dy:
                best, best_dy = pid, dy
        return best

    def is_walkable(self, poly_id: int) -> bool:
        return poly_id in self.polys and poly_id not in self.blocked

    def is_dirty(self, poly_id: int) -> bool:
        return poly_id in self.dirty

    def polys_intersecting(self, box: AABB) -> List[int]:
        return sorted(pid for pid, poly in self.polys.items()
                      if poly.bounds().intersects(box))

    def nbytes(self) -> int:
        return sum(len(p.points) * 8 + 32 for p in self.polys.values())

    # -- pathing -------------------------------------------------------------
    def find_path(self, start: int, goal: int, *,
                  avoid_dirty: bool = True) -> Optional[List[int]]:
        """Dijkstra over polygon centres, ties broken by id.

        Deterministic for the same reason Octopus's graph search is: equal-cost
        ties resolve by id, so the chosen route is reproducible run to run.

        `avoid_dirty` is the Scope 10 rule in one flag. A path is not allowed to
        route through a polygon whose walkability is still being recomputed;
        the caller holds position instead of committing to a route that may be
        invalidated a frame later.
        """
        if start not in self.polys or goal not in self.polys:
            return None
        if not self._passable(start, avoid_dirty) or not self._passable(goal, avoid_dirty):
            return None
        if start == goal:
            return [start]
        dist: Dict[int, float] = {start: 0.0}
        prev: Dict[int, int] = {}
        heap: List[Tuple[float, int]] = [(0.0, start)]
        seen: Set[int] = set()
        while heap:
            cost, pid = heapq.heappop(heap)
            if pid in seen:
                continue
            seen.add(pid)
            if pid == goal:
                break
            here = self.polys[pid].center()
            for nid in self.polys[pid].neighbours:
                if nid not in self.polys or nid in seen:
                    continue
                if not self._passable(nid, avoid_dirty):
                    continue
                step = cost + distance(here, self.polys[nid].center())
                if step < dist.get(nid, float("inf")):
                    dist[nid] = step
                    prev[nid] = pid
                    heapq.heappush(heap, (step, nid))
        if goal not in seen:
            return None
        out = [goal]
        while out[-1] != start:
            out.append(prev[out[-1]])
        out.reverse()
        return out

    def _passable(self, poly_id: int, avoid_dirty: bool) -> bool:
        if poly_id in self.blocked:
            return False
        if avoid_dirty and poly_id in self.dirty:
            return False
        return True

    def path_touches_dirty(self, path: Iterable[int]) -> bool:
        return any(pid in self.dirty for pid in path)

    def reachable(self, start: int, goal: int, *,
                  avoid_dirty: bool = True) -> bool:
        return self.find_path(start, goal, avoid_dirty=avoid_dirty) is not None

    # -- patching ------------------------------------------------------------
    def mark_dirty(self, poly_ids: Iterable[int]) -> List[int]:
        marked = [pid for pid in poly_ids if pid in self.polys]
        self.dirty.update(marked)
        return sorted(marked)

    def apply_patch(self, walkability: Dict[int, bool]) -> List[int]:
        """Commit a completed recompute. Clears dirty for the polys it covers."""
        changed: List[int] = []
        for pid, walkable in sorted(walkability.items()):
            if pid not in self.polys:
                continue
            was = pid not in self.blocked
            if walkable and not was:
                self.blocked.discard(pid)
                changed.append(pid)
            elif not walkable and was:
                self.blocked.add(pid)
                changed.append(pid)
            self.dirty.discard(pid)
        self.version += 1
        return changed

    def clone(self) -> "Navmesh":
        """A fresh navmesh with the same baked polys and no runtime state.

        The baked mesh in a `CellBundle` is a template: `blocked` and `dirty`
        belong to one residency, and carrying them into the next load would be
        break-state kept outside `StructureState`, which L5 forbids.
        """
        return Navmesh(self.cell_id, list(self.polys.values()))

    def to_dict(self) -> Dict[str, Any]:
        return {"cell_id": self.cell_id,
                "polys": [self.polys[pid].to_dict() for pid in sorted(self.polys)]}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Navmesh":
        return cls(raw["cell_id"], [NavPoly.from_dict(p) for p in raw.get("polys", ())])


# ---------------------------------------------------------------------------
# Load-bearing inference results (produced by the build step, read at runtime)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoadBearingTable:
    """Per structure, which micro-chunks touch the navmesh and which polys.

    `inferred` is what the build step computed; `overrides` is the author's
    opt-out for the rare decorative case. Both are carried so a build report
    can show a writer what was inferred and what they overrode - the inference
    only fails safe if it is visible.
    """

    structure_id: str
    inferred: Dict[int, Tuple[int, ...]] = dc_field(default_factory=dict)
    overrides: Dict[int, bool] = dc_field(default_factory=dict)

    def is_load_bearing(self, chunk_index: int) -> bool:
        if chunk_index in self.overrides:
            return bool(self.overrides[chunk_index])
        return bool(self.inferred.get(chunk_index))

    def polys_for(self, chunk_index: int) -> Tuple[int, ...]:
        if self.overrides.get(chunk_index) is False:
            return ()
        return tuple(self.inferred.get(chunk_index, ()))

    def to_dict(self) -> Dict[str, Any]:
        return {"structure_id": self.structure_id,
                "inferred": {str(k): list(v) for k, v in sorted(self.inferred.items())},
                "overrides": {str(k): v for k, v in sorted(self.overrides.items())}}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LoadBearingTable":
        return cls(structure_id=raw["structure_id"],
                   inferred={int(k): tuple(int(p) for p in v)
                             for k, v in (raw.get("inferred") or {}).items()},
                   overrides={int(k): bool(v)
                              for k, v in (raw.get("overrides") or {}).items()})


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------

def recompute_polys(navmesh: Navmesh, poly_ids: Iterable[int], *,
                    terrain: Any = None,
                    structures: Iterable[Any] = (),
                    agent_height: float = DEFAULT_AGENT_HEIGHT_M,
                    support_probe: float = DEFAULT_SUPPORT_PROBE_M,
                    voxel_size: Optional[float] = None) -> Dict[int, bool]:
    """Re-derive walkability for a set of polys from the geometry that is there
    now. Pure: it reads, it returns a verdict, it mutates nothing.

    A polygon is walkable when, at its sample points, something supports it from
    just below (terrain at that height, or a surviving structure voxel) and
    nothing solid occupies the agent's clearance above it.

    Both directions fall out of that without a special case: bring down the
    wall standing in a doorway and the poly becomes walkable; bring down the
    deck under a bridge and it stops being.
    """
    from .constants import VOXEL_SIZE_M
    vsize = VOXEL_SIZE_M if voxel_size is None else voxel_size
    structures = list(structures)
    out: Dict[int, bool] = {}
    for pid in poly_ids:
        poly = navmesh.polys.get(pid)
        if poly is None:
            continue
        out[pid] = _poly_walkable(poly, terrain, structures, agent_height,
                                  support_probe, vsize)
    return out


def _sample_points(poly: NavPoly) -> List[Tuple[float, float]]:
    """Centre plus each vertex pulled slightly inward.

    Sampling only the centre lets a chunk take out half a polygon without the
    recompute noticing; sampling the raw vertices puts probes exactly on the
    shared edge with the neighbour, where both answers are defensible. Pulling
    in 20% is the cheap fix for both.
    """
    cx = sum(p[0] for p in poly.points) / len(poly.points)
    cz = sum(p[1] for p in poly.points) / len(poly.points)
    pts = [(cx, cz)]
    for x, z in poly.points:
        pts.append((x + (cx - x) * 0.2, z + (cz - z) * 0.2))
    return pts


def _poly_walkable(poly: NavPoly, terrain: Any, structures: Sequence[Any],
                   agent_height: float, support_probe: float,
                   voxel_size: float) -> bool:
    supported_any = False
    for x, z in _sample_points(poly):
        if _blocked_above(x, poly.y, z, structures, agent_height, voxel_size):
            return False
        if _supported(x, poly.y, z, terrain, structures, support_probe, voxel_size):
            supported_any = True
    return supported_any


def _solid_at(point: Vec3, structures: Sequence[Any], voxel_size: float) -> bool:
    for live in structures:
        data = live.voxel_data
        local = data.origin.inverse_apply(point)
        ix = int(local[0] // voxel_size)
        iy = int(local[1] // voxel_size)
        iz = int(local[2] // voxel_size)
        g = data.grid_size
        if 0 <= ix < g and 0 <= iy < g and 0 <= iz < g and live.is_solid(ix, iy, iz):
            return True
    return False


def _blocked_above(x: float, y: float, z: float, structures: Sequence[Any],
                   agent_height: float, voxel_size: float) -> bool:
    steps = max(1, int(agent_height / voxel_size))
    for i in range(steps):
        probe = (x, y + voxel_size * (i + 0.5), z)
        if _solid_at(probe, structures, voxel_size):
            return True
    return False


def _supported(x: float, y: float, z: float, terrain: Any,
               structures: Sequence[Any], support_probe: float,
               voxel_size: float) -> bool:
    if terrain is not None:
        try:
            ground = terrain.collider.ground_height(x, z)
        except Exception:  # a cell may legitimately have no collider
            ground = None
        if ground is not None and abs(ground - y) <= support_probe:
            return True
    steps = max(1, int(support_probe / voxel_size))
    for i in range(steps):
        probe = (x, y - voxel_size * (i + 0.5), z)
        if _solid_at(probe, structures, voxel_size):
            return True
    return False


# ---------------------------------------------------------------------------
# Deferred patching
# ---------------------------------------------------------------------------

@dataclass
class PatchJob:
    """One pending navmesh recompute. Carries why it exists, for the report."""

    cell_id: str
    poly_ids: Tuple[int, ...]
    cause_structure_id: Optional[str] = None
    cause_chunks: Tuple[int, ...] = ()
    #: cells whose connections into `cell_id` may be affected (Scope 10.3).
    adjacent_cell_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"cell_id": self.cell_id, "poly_ids": list(self.poly_ids),
                "cause_structure_id": self.cause_structure_id,
                "cause_chunks": list(self.cause_chunks),
                "adjacent_cell_ids": list(self.adjacent_cell_ids)}


class PatchQueue:
    """Deferred navmesh patches. Cannot block, by design.

    There is no `wait`, no `flush_now`, and no way for the collider path to
    reach this object - Scope 15.11: "Any shortcut that makes the voxel collider
    update wait on the navmesh patch (or vice versa) reintroduces the 10 race."
    The queue is pumped with a per-frame budget; until a job is pumped, its
    polys stay dirty and pathing routes around them or holds.
    """

    def __init__(self) -> None:
        self.pending: List[PatchJob] = []
        self.completed: List[Dict[str, Any]] = []

    def submit(self, job: PatchJob, navmesh: Navmesh) -> PatchJob:
        navmesh.mark_dirty(job.poly_ids)
        self.pending.append(job)
        return job

    def __len__(self) -> int:
        return len(self.pending)

    def pump(self, resolve, *, max_jobs: int = 1) -> List[Dict[str, Any]]:
        """Run up to `max_jobs` pending recomputes.

        `resolve(job) -> {poly_id: walkable}` is supplied by the cell manager,
        which is the only thing that knows which terrain and which live
        structures a cell currently holds.
        """
        done: List[Dict[str, Any]] = []
        for _ in range(max_jobs):
            if not self.pending:
                break
            job = self.pending.pop(0)
            verdicts = resolve(job)
            record = {"job": job.to_dict(), "walkability": verdicts}
            self.completed.append(record)
            done.append(record)
        return done
