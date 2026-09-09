"""Per-cell uniform spatial grid (Scope 7).

> **Spatial index: uniform grid, locked as the default (v0.4).** A BVH is the
> general-purpose answer, but Lobster's world isn't general-purpose [...] A
> uniform grid tuned to average entity spacing (proposal: **2.5 m**, adjustable
> per-cell) gives O(1) insertion and O(1) query [...] **BVH is explicitly not
> the default; it's a documented fallback.**

So there is one index here, it is a grid, and the BVH is not written. Scope
15.8 says the trigger for reconsidering is profiling a real outlier cell, not a
hunch - `QueryStats` exists so that profiling has numbers to look at.

**The v0.4 stale-snapshot fix is the other half of this module.**

> the per-cell spatial index takes a transform snapshot on cell entry and on
> cell exit - not continuously for dormant entities - so a PROJECTILE-tier
> query is always testing against the last *known-correct* position

and the failure mode that reopens it, from 15.10:

> If a future change adds a code path that moves a dormant entity (a scripted
> relocation, a mod effect) without also refreshing its grid snapshot,
> PROJECTILE-tier queries silently go stale again.

The API is shaped so that code path cannot be written by accident:

* every entry is created **with** a transform - there is no "insert then set
  position later", so a never-set value cannot exist;
* `move` refuses to move a non-ACTIVE entity, and says to call
  `refresh_snapshot` instead;
* `refresh_snapshot` is the *only* way a dormant entity's position changes, and
  it stamps a new snapshot sequence and reason;
* every query result carries the snapshot sequence it was resolved against, so
  a consumer can tell how old the answer is instead of assuming it is live.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple)

from .constants import EXTERIOR_CELL_SIZE_M, SPATIAL_GRID_CELL_M
from .geometry import Vec3, distance
from .tiers import ACTIVE, DORMANT, PROJECTILE, check as check_tier

#: Why a snapshot was taken. Scope 7 names the first two; the third exists for
#: the "scripted relocation or mod effect" case 15.10 warns about, so that path
#: has a supported way to stay honest instead of writing the grid directly.
SNAPSHOT_CELL_ENTRY = "cell_entry"
SNAPSHOT_CELL_EXIT = "cell_exit"
SNAPSHOT_EXPLICIT = "explicit_refresh"


class SpatialError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    """One entity's place in the grid, and the provenance of that place."""

    entity_id: str
    position: Vec3
    tier: str
    snapshot_seq: int
    snapshot_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"entity_id": self.entity_id, "position": list(self.position),
                "tier": self.tier, "snapshot_seq": self.snapshot_seq,
                "snapshot_reason": self.snapshot_reason}


@dataclass
class QueryStats:
    """What a query actually cost. The input to any future BVH argument.

    **`buckets_scanned` is the one that tracks time; `buckets_visited` is the
    one that tracks population.** A grid walk pays for every bucket key it
    forms and looks up, empty or not, and on a long ray through a thin crowd
    the empty ones are almost all of them. Counting only the buckets that had
    somebody in them measures how crowded the cell is, not how much work the
    query did - which is why the two are now separate (DECISIONS.md D27).
    """

    queries: int = 0
    #: bucket keys formed and looked up - the traversal cost, empty or not
    buckets_scanned: int = 0
    #: of those, the ones that held at least one entity
    buckets_visited: int = 0
    candidates_considered: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"queries": self.queries,
                "buckets_scanned": self.buckets_scanned,
                "buckets_visited": self.buckets_visited,
                "candidates_considered": self.candidates_considered}


@dataclass(frozen=True)
class Candidate:
    """A query hit, with the snapshot it was resolved against."""

    entity_id: str
    position: Vec3
    tier: str
    snapshot_seq: int
    snapshot_reason: str
    distance: float

    def to_dict(self) -> Dict[str, Any]:
        return {"entity_id": self.entity_id, "position": list(self.position),
                "tier": self.tier, "snapshot_seq": self.snapshot_seq,
                "snapshot_reason": self.snapshot_reason,
                "distance": self.distance}


class SpatialIndex:
    """Uniform grid over a cell's XZ extent.

    XZ rather than XYZ: entities stand on the ground, a cell is 128 m across and
    nothing like 128 m tall, and a third axis would buy empty buckets. Vertical
    separation is handled by the detailed hit test, which is where it belongs.
    """

    def __init__(self, cell_id: str, *,
                 cell_size_m: float = SPATIAL_GRID_CELL_M,
                 extent_m: float = EXTERIOR_CELL_SIZE_M,
                 origin: Tuple[float, float] = (0.0, 0.0)) -> None:
        if cell_size_m <= 0:
            raise SpatialError(
                "cell {0!r}: spatial grid cell size must be positive".format(cell_id))
        self.cell_id = cell_id
        self.cell_size_m = float(cell_size_m)
        self.extent_m = float(extent_m)
        self.origin = origin
        self.dim = max(1, int(self.extent_m / self.cell_size_m) + 1)
        self._buckets: Dict[Tuple[int, int], Set[str]] = {}
        self._entries: Dict[str, Entry] = {}
        self.snapshot_seq = 0
        self.stats = QueryStats()

    # -- bucketing -----------------------------------------------------------
    def _bucket(self, position: Vec3) -> Tuple[int, int]:
        ix = int((position[0] - self.origin[0]) // self.cell_size_m)
        iz = int((position[2] - self.origin[1]) // self.cell_size_m)
        return (min(max(ix, 0), self.dim - 1), min(max(iz, 0), self.dim - 1))

    def bucket_count(self) -> int:
        return len(self._buckets)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, entity_id: str) -> bool:
        return entity_id in self._entries

    def entry(self, entity_id: str) -> Entry:
        e = self._entries.get(entity_id)
        if e is None:
            raise SpatialError(
                "cell {0!r}: no spatial entry for {1!r}. An entity is only ever "
                "inserted together with a transform, so this means it was never "
                "snapshotted into this cell.".format(self.cell_id, entity_id))
        return e

    def entries(self) -> List[Entry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def tier_of(self, entity_id: str) -> str:
        return self.entry(entity_id).tier

    # -- snapshots -----------------------------------------------------------
    def snapshot(self, entities: Iterable[Tuple[str, Vec3, str]], *,
                 reason: str = SNAPSHOT_CELL_ENTRY) -> int:
        """Replace the whole index from a list of (entity_id, position, tier).

        Called on cell entry and on cell exit, and nowhere in between for
        dormant entities - which is exactly the point: the grid stays cheap
        because nobody is ticking positions nobody is simulating, and it stays
        honest because what is in it is the last known-correct transform rather
        than whatever was there when the cell went away.
        """
        self.snapshot_seq += 1
        self._buckets.clear()
        self._entries.clear()
        for entity_id, position, tier in entities:
            self._insert(entity_id, position, tier, reason)
        return self.snapshot_seq

    def _insert(self, entity_id: str, position: Vec3, tier: str,
                reason: str) -> Entry:
        if position is None:
            raise SpatialError(
                "cell {0!r}: {1!r} has no transform. An entry without a "
                "position is the stale/never-set value Scope 7 exists to "
                "prevent.".format(self.cell_id, entity_id))
        check_tier(tier)
        entry = Entry(entity_id=entity_id, position=tuple(float(c) for c in position),
                      tier=tier, snapshot_seq=self.snapshot_seq,
                      snapshot_reason=reason)
        self._entries[entity_id] = entry
        self._buckets.setdefault(self._bucket(entry.position), set()).add(entity_id)
        return entry

    def add(self, entity_id: str, position: Vec3, tier: str, *,
            reason: str = SNAPSHOT_CELL_ENTRY) -> Entry:
        """Insert one entity. Position is mandatory; there is no two-step form."""
        if entity_id in self._entries:
            self.remove(entity_id)
        return self._insert(entity_id, position, tier, reason)

    def remove(self, entity_id: str) -> None:
        entry = self._entries.pop(entity_id, None)
        if entry is None:
            return
        bucket = self._buckets.get(self._bucket(entry.position))
        if bucket is not None:
            bucket.discard(entity_id)
            if not bucket:
                self._buckets.pop(self._bucket(entry.position), None)

    def refresh_snapshot(self, entity_id: str, position: Vec3, *,
                         tier: Optional[str] = None,
                         reason: str = SNAPSHOT_EXPLICIT) -> Entry:
        """Re-snapshot one entity's transform, whatever its tier.

        The supported answer to 15.10. A scripted relocation or a mod effect
        that moves a dormant entity calls this; the entry gets a fresh snapshot
        sequence, and PROJECTILE-tier queries keep resolving against a
        known-correct position instead of going quietly stale.
        """
        current = self._entries.get(entity_id)
        self.snapshot_seq += 1
        return self.add(entity_id, position,
                        tier or (current.tier if current else DORMANT),
                        reason=reason)

    def move(self, entity_id: str, position: Vec3) -> Entry:
        """Continuous position update. ACTIVE-tier entities only.

        Refusing this for the other tiers is the whole guard: a dormant entity
        is by definition one nobody is ticking, so a caller that has a new
        position for one got it from somewhere other than simulation, and it
        needs to say so via `refresh_snapshot` - which stamps the provenance
        that makes the staleness visible.
        """
        entry = self.entry(entity_id)
        if entry.tier != ACTIVE:
            raise SpatialError(
                "cell {0!r}: {1!r} is at tier {2}, not {3}; a non-ACTIVE entity's "
                "position is a snapshot, not a live value. Use "
                "refresh_snapshot() so the new transform carries its provenance "
                "(Scope 7 / 15.10).".format(self.cell_id, entity_id, entry.tier,
                                            ACTIVE))
        self.remove(entity_id)
        return self._insert(entity_id, position, ACTIVE, entry.snapshot_reason)

    def set_tier(self, entity_id: str, tier: str) -> Entry:
        entry = self.entry(entity_id)
        check_tier(tier)
        self.remove(entity_id)
        return self._insert(entity_id, entry.position, tier,
                            entry.snapshot_reason)

    # -- queries -------------------------------------------------------------
    def _span(self, radius: float) -> int:
        """How many buckets out from a touched bucket a candidate can hide.

        With `|a - b| <= r`, two bucket indices differ by `d` only if
        `(d - 1) * cell < r`, so `d < r/cell + 1` and the tightest safe span is
        **`ceil(r / cell)`**.

        It used to be `int(r / cell) + 1`, which is the same number except when
        `r / cell` is an integer - and that is precisely the shipping case:
        `broad_margin()` floors at `PROJECTILE_BROAD_RADIUS_M` (2.5 m) and the
        grid is `SPATIAL_GRID_CELL_M` (2.5 m), so the ratio is exactly 1.0 and
        every touched bucket was dilated into a 5x5 neighbourhood where 3x3 is
        sufficient - 25 bucket scans per step instead of 9.

        Floored at 1 because `_segment_buckets` samples the line rather than
        supercovering it, so a diagonal can cross a bucket no sample lands in.
        Consecutive samples are at most one bucket apart, so any skipped bucket
        is adjacent to a sampled one and a span of 1 catches it.
        """
        return max(1, math.ceil(radius / self.cell_size_m))

    def query_sphere(self, center: Vec3, radius: float, *,
                     tiers: Optional[Sequence[str]] = None) -> List[Candidate]:
        """Candidates within `radius`. O(buckets touched), not O(entities).

        This is the "small candidate set" step: no hitbox, no skeleton, no
        Octopus query. Detailed testing happens afterwards, on whatever comes
        back, at whatever fidelity the tier declares.
        """
        self.stats.queries += 1
        allowed = set(tiers) if tiers else None
        span = self._span(radius)
        cx, cz = self._bucket(center)
        out: List[Candidate] = []
        for ix in range(cx - span, cx + span + 1):
            for iz in range(cz - span, cz + span + 1):
                self.stats.buckets_scanned += 1
                bucket = self._buckets.get((ix, iz))
                if not bucket:
                    continue
                self.stats.buckets_visited += 1
                for entity_id in bucket:
                    entry = self._entries[entity_id]
                    self.stats.candidates_considered += 1
                    if allowed is not None and entry.tier not in allowed:
                        continue
                    d = distance(center, entry.position)
                    if d <= radius:
                        out.append(Candidate(
                            entity_id=entry.entity_id, position=entry.position,
                            tier=entry.tier, snapshot_seq=entry.snapshot_seq,
                            snapshot_reason=entry.snapshot_reason, distance=d))
        out.sort(key=lambda c: (c.distance, c.entity_id))
        return out

    def query_segment(self, start: Vec3, end: Vec3, radius: float, *,
                      tiers: Optional[Sequence[str]] = None) -> List[Candidate]:
        """Candidates within `radius` of a segment - a swing arc, or a
        projectile's path this frame.

        **Walks the grid along the segment.** The obvious implementation -
        bound the segment with a sphere and reuse `query_sphere` - is quadratic
        in range and was measurably wrong here: a 100 m arrow becomes a 50 m
        sphere, which at a 2.5 m grid is ~1,250 buckets and, in a packed
        battlefield cell, returns *every entity in the cell* as a broad
        candidate before a single detailed test runs. A volley then costs
        O(arrows x army), which is exactly the population scaling Scope 7
        exists to avoid:

        > Cost scales with attacks and tiers, not population.

        Sampling the segment and dilating by the query radius makes the cost
        O(length / cell_size), independent of how many entities are standing
        near it. See DECISIONS.md D16.
        """
        from .geometry import closest_point_on_segment
        self.stats.queries += 1
        allowed = set(tiers) if tiers else None
        span = self._span(radius)

        seen_buckets: Set[Tuple[int, int]] = set()
        seen_entities: Set[str] = set()
        out: List[Candidate] = []

        for centre in self._segment_buckets(start, end):
            for ix in range(centre[0] - span, centre[0] + span + 1):
                for iz in range(centre[1] - span, centre[1] + span + 1):
                    key = (ix, iz)
                    if key in seen_buckets:
                        continue
                    seen_buckets.add(key)
                    self.stats.buckets_scanned += 1
                    bucket = self._buckets.get(key)
                    if not bucket:
                        continue
                    self.stats.buckets_visited += 1
                    for entity_id in bucket:
                        if entity_id in seen_entities:
                            continue
                        seen_entities.add(entity_id)
                        entry = self._entries[entity_id]
                        self.stats.candidates_considered += 1
                        if allowed is not None and entry.tier not in allowed:
                            continue
                        _, d = closest_point_on_segment(start, end,
                                                        entry.position)
                        if d <= radius:
                            out.append(Candidate(
                                entity_id=entry.entity_id,
                                position=entry.position, tier=entry.tier,
                                snapshot_seq=entry.snapshot_seq,
                                snapshot_reason=entry.snapshot_reason,
                                distance=d))
        out.sort(key=lambda c: (c.distance, c.entity_id))
        return out

    def _segment_buckets(self, start: Vec3, end: Vec3) -> List[Tuple[int, int]]:
        """Bucket coordinates the segment passes through, on the XZ plane.

        Sampled at half a cell so no bucket the line crosses is skipped, which
        is cheaper than a full supercover DDA and cannot miss: two consecutive
        samples are never more than one bucket apart.
        """
        dx = end[0] - start[0]
        dz = end[2] - start[2]
        planar = math.sqrt(dx * dx + dz * dz)
        steps = int(planar / (self.cell_size_m * 0.5)) + 1
        out: List[Tuple[int, int]] = []
        seen: Set[Tuple[int, int]] = set()
        for i in range(steps + 1):
            t = i / steps
            point = (start[0] + dx * t, start[1], start[2] + dz * t)
            key = self._bucket(point)
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    # -- accounting ----------------------------------------------------------
    def active_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.tier == ACTIVE)

    def nbytes(self) -> int:
        return len(self._entries) * 96 + len(self._buckets) * 64

    def report(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {ACTIVE: 0, PROJECTILE: 0, DORMANT: 0}
        for e in self._entries.values():
            counts[e.tier] = counts.get(e.tier, 0) + 1
        return {"cell_id": self.cell_id, "grid_cell_m": self.cell_size_m,
                "dim": self.dim, "entities": len(self._entries),
                "occupied_buckets": len(self._buckets),
                "snapshot_seq": self.snapshot_seq, "by_tier": counts,
                "stats": self.stats.to_dict()}
