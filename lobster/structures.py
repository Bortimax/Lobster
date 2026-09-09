"""Structure voxel data and break-state (Scope 6).

Two objects, and the whole point is that they are two:

``StructureVoxelData``
    The authored grid. Content. Ships in the `.lobster_cell` bundle, is
    immutable, and is **never saved**. Shared by reference between every
    instance of the same structure - a keep authored once costs its voxels
    once.

``LiveStructure``
    The authored grid plus the set of destroyed micro-chunk indices resolved
    from the `StructureState` record. Damage never edits the authored grid; it
    adds a chunk index to a set, and the mesher consults that set. "Cost
    proportional to hits landed, never structure size" (Scope 3) is a property
    of doing it this way and no other.

Terrain is not in this module and never will be (L2). There is no shared base
class, no shared mesher entry point, and no shared memory pool; the section 14
separation test asserts it.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .constants import MICRO_CHUNK_VOXELS, VOXEL_SIZE_M
from .geometry import AABB, Transform, Vec3

#: `destroyed_chunks: Set<u16>` (Scope 6) - so a structure may not have more
#: micro-chunks than a u16 can index.
MAX_CHUNK_INDEX = 0xFFFF


class StructureError(Exception):
    """Malformed authored structure data. Names the structure."""


@dataclass(frozen=True)
class StructureVoxelData:
    """The authored grid - content, ships in the bundle, never saved.

    Wire format, verbatim from Scope 6:

        structure_id:  stable id, assigned at authoring time
        grid_size:     u16  (multiple of chunk_size)
        chunk_size:    u8   (default 8)
        material_ids:  []u8, length grid_size^3 (palette index, not per-voxel colour)
        origin:        position + rotation within the cell
    """

    structure_id: str
    grid_size: int
    material_ids: bytes
    origin: Transform = dc_field(default_factory=Transform)
    chunk_size: int = MICRO_CHUNK_VOXELS
    #: palette index 0 is empty space by convention; anything else is solid.
    empty_material: int = 0

    def __post_init__(self) -> None:
        sid = self.structure_id
        if not isinstance(sid, str) or not sid:
            raise StructureError("structure_id must be a non-empty string")
        if not isinstance(self.grid_size, int) or self.grid_size <= 0:
            raise StructureError(
                "{0}: grid_size must be a positive integer, got "
                "{1!r}".format(sid, self.grid_size))
        if self.grid_size > 0xFFFF:
            raise StructureError(
                "{0}: grid_size {1} exceeds the u16 wire format".format(
                    sid, self.grid_size))
        if not isinstance(self.chunk_size, int) or not (0 < self.chunk_size <= 0xFF):
            raise StructureError(
                "{0}: chunk_size must be a u8 greater than zero, got "
                "{1!r}".format(sid, self.chunk_size))
        if self.grid_size % self.chunk_size:
            raise StructureError(
                "{0}: grid_size {1} is not a multiple of chunk_size {2} "
                "(Scope 6)".format(sid, self.grid_size, self.chunk_size))
        expected = self.grid_size ** 3
        if len(self.material_ids) != expected:
            raise StructureError(
                "{0}: material_ids has {1} entries, expected grid_size^3 = "
                "{2}".format(sid, len(self.material_ids), expected))
        if self.chunk_count > MAX_CHUNK_INDEX + 1:
            raise StructureError(
                "{0}: {1} micro-chunks exceeds the u16 index space that "
                "StructureState.destroyed_chunks declares (max {2}); author "
                "this as several structures".format(
                    sid, self.chunk_count, MAX_CHUNK_INDEX + 1))

    # -- shape ---------------------------------------------------------------
    @property
    def chunks_per_axis(self) -> int:
        return self.grid_size // self.chunk_size

    @property
    def chunk_count(self) -> int:
        return self.chunks_per_axis ** 3

    @property
    def voxel_count(self) -> int:
        return self.grid_size ** 3

    def solid_voxel_count(self) -> int:
        return sum(1 for m in self.material_ids if m != self.empty_material)

    def nbytes(self) -> int:
        """Declared residency cost of the authored grid."""
        return len(self.material_ids)

    # -- indexing ------------------------------------------------------------
    def voxel_index(self, x: int, y: int, z: int) -> int:
        g = self.grid_size
        return x + y * g + z * g * g

    def material_at(self, x: int, y: int, z: int) -> int:
        return self.material_ids[self.voxel_index(x, y, z)]

    def is_solid(self, x: int, y: int, z: int) -> bool:
        g = self.grid_size
        if not (0 <= x < g and 0 <= y < g and 0 <= z < g):
            return False
        return self.material_ids[self.voxel_index(x, y, z)] != self.empty_material

    def chunk_index(self, cx: int, cy: int, cz: int) -> int:
        c = self.chunks_per_axis
        return cx + cy * c + cz * c * c

    def chunk_coords(self, index: int) -> Tuple[int, int, int]:
        c = self.chunks_per_axis
        cx = index % c
        cy = (index // c) % c
        cz = index // (c * c)
        return cx, cy, cz

    def chunk_of_voxel(self, x: int, y: int, z: int) -> int:
        s = self.chunk_size
        return self.chunk_index(x // s, y // s, z // s)

    def chunk_indices(self) -> range:
        return range(self.chunk_count)

    def voxels_in_chunk(self, index: int) -> Iterator[Tuple[int, int, int]]:
        cx, cy, cz = self.chunk_coords(index)
        s = self.chunk_size
        for z in range(cz * s, cz * s + s):
            for y in range(cy * s, cy * s + s):
                for x in range(cx * s, cx * s + s):
                    yield x, y, z

    def chunk_is_empty(self, index: int) -> bool:
        return not any(self.is_solid(x, y, z)
                       for x, y, z in self.voxels_in_chunk(index))

    # -- placement -----------------------------------------------------------
    def chunk_aabb_local(self, index: int,
                         voxel_size: float = VOXEL_SIZE_M) -> AABB:
        cx, cy, cz = self.chunk_coords(index)
        s = self.chunk_size * voxel_size
        lo = (cx * s, cy * s, cz * s)
        return AABB(lo, (lo[0] + s, lo[1] + s, lo[2] + s))

    def chunk_aabb_world(self, index: int,
                         voxel_size: float = VOXEL_SIZE_M) -> AABB:
        """Chunk bounds in cell space.

        Conservative under rotation: the eight rotated corners are re-bounded
        axis-aligned. The build step's navmesh-intersection inference (Scope
        10) uses this, and over-flagging a chunk there costs a recompute that
        turns out to be unnecessary, while under-flagging costs NPCs walking
        through rubble - so the conservative direction is the correct one.
        """
        local = self.chunk_aabb_local(index, voxel_size)
        lo, hi = local.minimum, local.maximum
        corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                   for z in (lo[2], hi[2])]
        return AABB.from_points(self.origin.apply(c) for c in corners)

    def aabb_world(self, voxel_size: float = VOXEL_SIZE_M) -> AABB:
        extent = self.grid_size * voxel_size
        corners = [(x, y, z) for x in (0.0, extent) for y in (0.0, extent)
                   for z in (0.0, extent)]
        return AABB.from_points(self.origin.apply(c) for c in corners)

    def to_bundle_dict(self) -> Dict[str, Any]:
        """Bundle representation. `material_ids` is carried out-of-band as raw
        bytes by the bundle writer; this is the header half."""
        return {"structure_id": self.structure_id, "grid_size": self.grid_size,
                "chunk_size": self.chunk_size,
                "empty_material": self.empty_material,
                "origin": self.origin.to_dict(),
                "material_ids_len": len(self.material_ids)}


# ---------------------------------------------------------------------------
# Break-state resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BreakStateResolution:
    """The result of reading a `StructureState` against an authored grid."""

    structure_id: str
    location_id: Optional[str]
    accepted: FrozenSet[int]
    quarantined: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"structure_id": self.structure_id,
                "location_id": self.location_id,
                "accepted": sorted(self.accepted),
                "quarantined": [dict(q) for q in self.quarantined]}


def resolve_break_state(voxel_data: StructureVoxelData,
                        state_record: Optional[Dict[str, Any]]
                        ) -> BreakStateResolution:
    """Read `StructureState.destroyed_chunks` against the current authored grid.

    Scope 6, "Bounds check on load (v0.4)":

    > a saved `destroyed_chunks` set built against the old `grid_size` can
    > contain indices that are out of range for the new one. Lobster must not
    > hand those straight to the mesher: on load, compute
    > `max(destroyed_chunks)` against the current chunk count, and if any index
    > is out of bounds, **quarantine those specific indices and log the
    > reason**, same as Octopus's own quarantine behavior for unresolvable save
    > ops - never crash the mesh builder, and never silently drop the whole
    > record when only a few indices are stale.

    So: per-index, not per-record. A record with one stale index keeps every
    good one. Malformed entries (non-integers, negatives) are quarantined the
    same way rather than raising, because they arrive from a save file that a
    removed mod may have written, and a crash there is unrecoverable for a
    player where a quarantine is not.
    """
    if state_record is None:
        return BreakStateResolution(structure_id=voxel_data.structure_id,
                                    location_id=None, accepted=frozenset())
    raw = state_record.get("destroyed_chunks") or []
    limit = voxel_data.chunk_count
    accepted: Set[int] = set()
    quarantined: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int):
            quarantined.append({
                "record_id": state_record.get("id"),
                "structure_id": voxel_data.structure_id,
                "value": entry,
                "reason": "destroyed_chunks entry is not an integer chunk "
                          "index"})
            continue
        if entry < 0 or entry >= limit:
            quarantined.append({
                "record_id": state_record.get("id"),
                "structure_id": voxel_data.structure_id,
                "value": entry,
                "reason": "chunk index {0} is out of range for the current "
                          "authored grid (grid_size {1}, chunk_size {2} -> "
                          "{3} chunks); a package may have shrunk or replaced "
                          "this structure".format(entry, voxel_data.grid_size,
                                                  voxel_data.chunk_size, limit)})
            continue
        accepted.add(entry)
    return BreakStateResolution(
        structure_id=voxel_data.structure_id,
        location_id=state_record.get("location_id"),
        accepted=frozenset(accepted),
        quarantined=tuple(quarantined))


# ---------------------------------------------------------------------------
# Live instance
# ---------------------------------------------------------------------------

class LiveStructure:
    """An authored grid plus its resolved break-state, resident in a cell.

    The authored grid is referenced, never copied. Destruction adds indices to
    a set and marks chunks dirty; nothing rewrites `material_ids`, so the cost
    of a hit is the cost of remeshing the chunks it touched.
    """

    def __init__(self, voxel_data: StructureVoxelData,
                 break_state: Optional[BreakStateResolution] = None,
                 *, location_id: Optional[str] = None) -> None:
        self.voxel_data = voxel_data
        self.location_id = location_id or (break_state.location_id
                                           if break_state else None)
        self.destroyed: Set[int] = set(break_state.accepted) if break_state else set()
        self.quarantined: Tuple[Dict[str, Any], ...] = (
            break_state.quarantined if break_state else ())
        #: chunks whose mesh is out of date. Seeded with the chunks the loaded
        #: break-state already removed, so cell load meshes "only the affected
        #: micro-chunks" (Scope 6).
        self.dirty_chunks: Set[int] = set(self.destroyed)
        #: bumped on every synchronous collider/mesh update. Scope 10's race
        #: test reads it: the visual mesh and the collider share this number,
        #: and the navmesh has its own.
        self.geometry_version = 0

    # -- identity ------------------------------------------------------------
    @property
    def structure_id(self) -> str:
        return self.voxel_data.structure_id

    def is_chunk_destroyed(self, index: int) -> bool:
        return index in self.destroyed

    def is_solid(self, x: int, y: int, z: int) -> bool:
        """Voxel solidity *after* break-state masking.

        This is the only reader of the two together, and the only thing the
        mesher and the collider are allowed to ask.
        """
        if not self.voxel_data.is_solid(x, y, z):
            return False
        return self.voxel_data.chunk_of_voxel(x, y, z) not in self.destroyed

    def surviving_chunks(self) -> List[int]:
        return [i for i in self.voxel_data.chunk_indices()
                if i not in self.destroyed]

    # -- damage --------------------------------------------------------------
    def destroy_chunks(self, indices: Iterable[int]) -> List[int]:
        """Mark chunks destroyed. Returns the ones that were not already gone.

        Idempotent, which is what makes UNION_TOMBSTONED the right merge policy
        upstream: applying the same destruction twice - two mods, a save
        reloaded, a world tick that re-resolves - changes nothing.

        Out-of-range indices raise here, unlike on load: at runtime the caller
        is Lobster itself computing a chunk from a hit position, so a bad index
        is a shell bug, not stale save data.
        """
        newly: List[int] = []
        limit = self.voxel_data.chunk_count
        for raw in indices:
            index = int(raw)
            if index < 0 or index >= limit:
                raise StructureError(
                    "{0}: chunk index {1} out of range 0..{2}".format(
                        self.structure_id, index, limit - 1))
            if index in self.destroyed:
                continue
            self.destroyed.add(index)
            newly.append(index)
        if newly:
            self.dirty_chunks.update(newly)
            self.dirty_chunks.update(self._neighbours_of(newly))
            self.geometry_version += 1
        return sorted(newly)

    def _neighbours_of(self, indices: Iterable[int]) -> Set[int]:
        """Face-adjacent chunks, which need remeshing because removing a chunk
        exposes their inner faces."""
        out: Set[int] = set()
        c = self.voxel_data.chunks_per_axis
        for index in indices:
            cx, cy, cz = self.voxel_data.chunk_coords(index)
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                               (0, 0, 1), (0, 0, -1)):
                nx, ny, nz = cx + dx, cy + dy, cz + dz
                if 0 <= nx < c and 0 <= ny < c and 0 <= nz < c:
                    n = self.voxel_data.chunk_index(nx, ny, nz)
                    if n not in self.destroyed:
                        out.add(n)
        return out

    def take_dirty(self) -> List[int]:
        """Drain the remesh queue."""
        out = sorted(self.dirty_chunks)
        self.dirty_chunks.clear()
        return out

    # -- spatial -------------------------------------------------------------
    def chunk_at_point(self, world_point: Vec3,
                       voxel_size: float = VOXEL_SIZE_M) -> Optional[int]:
        """Which micro-chunk contains this cell-space point, if any."""
        local = self.voxel_data.origin.inverse_apply(world_point)
        s = self.voxel_data.chunk_size * voxel_size
        c = self.voxel_data.chunks_per_axis
        coords = []
        for axis in range(3):
            i = int(local[axis] // s)
            if i < 0 or i >= c:
                return None
            coords.append(i)
        return self.voxel_data.chunk_index(*coords)

    def chunks_in_sphere(self, center: Vec3, radius: float,
                         voxel_size: float = VOXEL_SIZE_M) -> List[int]:
        """Surviving chunks whose bounds fall within a blast radius.

        Used by both the witnessed path and section 6.5's world-tick path, so a
        town fireballed while the player is elsewhere rubbles the same way it
        would have with someone watching.
        """
        from .geometry import sphere_aabb_overlap
        out: List[int] = []
        for index in self.surviving_chunks():
            box = self.voxel_data.chunk_aabb_world(index, voxel_size)
            if sphere_aabb_overlap(center, radius, box):
                out.append(index)
        return out

    # -- accounting ----------------------------------------------------------
    def nbytes(self) -> int:
        """This instance's own cost - NOT the authored grid's.

        The grid is shared content charged once per cell by the loader; an
        instance costs its break-state set. Charging the grid per instance
        would make a cell with two copies of the same keep look twice as
        expensive as it is, and budget numbers that lie are worse than none.
        """
        return 2 * (len(self.destroyed) + len(self.dirty_chunks)) + 64

    def state_dict(self) -> Dict[str, Any]:
        return {"structure_id": self.structure_id,
                "location_id": self.location_id,
                "destroyed_chunks": sorted(self.destroyed),
                "quarantined": [dict(q) for q in self.quarantined],
                "geometry_version": self.geometry_version}
