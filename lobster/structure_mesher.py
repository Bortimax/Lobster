"""Runtime greedy meshing for structures, one micro-chunk at a time (Scope 3).

> Turning a voxel grid into a mesh without a triangle explosion -> **Greedy
> meshing**. Build-time for terrain; touched-micro-chunk-only at runtime for
> structures.
>
> Runtime cost of a destructible object -> **Micro-chunking** (8^3 voxels
> default). Cost proportional to hits landed, never structure size.

**This is a second implementation of greedy meshing on purpose.** The terrain
mesher lives in `lobster.build.terrain_mesher` and shares nothing with this
file - not a helper, not a quad type, not an import. Scope 15.1: "Don't let
terrain and structures share a code path", and L2 puts it more strongly still.
The duplication is the cost of that rule, it is small, and it is written down
in DECISIONS.md D11 rather than quietly avoided.

The two are not the same job anyway. Terrain is meshed once, offline, whole-cell,
from an authored grid that will never change. This runs inside a frame, over one
8^3 chunk, against solidity that is masked by break-state - and it has to get
the chunk *boundary* right, because the whole point of destroying a chunk is
that its neighbours' inner faces become visible.

Solidity is read through `LiveStructure.is_solid`, which applies the break-state
mask, and never through the authored grid directly. That is what makes a
destroyed chunk disappear without anybody rewriting `material_ids`.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import VOXEL_SIZE_M
from .geometry import Vec3

#: (normal, axis, sign) per cube face. Axis 0/1/2 is x/y/z.
_FACES: Tuple[Tuple[Vec3, int, int], ...] = (
    ((1.0, 0.0, 0.0), 0, +1),
    ((-1.0, 0.0, 0.0), 0, -1),
    ((0.0, 1.0, 0.0), 1, +1),
    ((0.0, -1.0, 0.0), 1, -1),
    ((0.0, 0.0, 1.0), 2, +1),
    ((0.0, 0.0, -1.0), 2, -1),
)


@dataclass(frozen=True)
class Quad:
    """One greedy-merged face rectangle, in structure-local metres."""

    material: int
    normal: Vec3
    corners: Tuple[Vec3, Vec3, Vec3, Vec3]

    def to_dict(self) -> Dict[str, Any]:
        return {"material": self.material, "normal": list(self.normal),
                "corners": [list(c) for c in self.corners]}


@dataclass(frozen=True)
class ChunkMesh:
    """The mesh of one micro-chunk. Replaced wholesale when the chunk changes."""

    structure_id: str
    chunk_index: int
    quads: Tuple[Quad, ...] = ()

    def triangle_count(self) -> int:
        return len(self.quads) * 2

    def nbytes(self) -> int:
        # 4 vertices * (3 position floats + 3 normal floats) * 4 bytes, plus
        # 6 indices * 4 bytes
        return len(self.quads) * (4 * 6 * 4 + 6 * 4)


def mesh_chunk(live: Any, chunk_index: int, *,
               voxel_size: float = VOXEL_SIZE_M) -> ChunkMesh:
    """Greedy-mesh one micro-chunk of a live structure.

    A destroyed chunk meshes to nothing, which is the cheapest possible answer
    and the correct one: the geometry is gone, and its neighbours are queued for
    remeshing separately (`LiveStructure.destroy_chunks` dirties them).
    """
    data = live.voxel_data
    if live.is_chunk_destroyed(chunk_index):
        return ChunkMesh(structure_id=live.structure_id,
                         chunk_index=chunk_index, quads=())

    size = data.chunk_size
    cx, cy, cz = data.chunk_coords(chunk_index)
    base = (cx * size, cy * size, cz * size)
    quads: List[Quad] = []

    for normal, axis, sign in _FACES:
        u_axis, v_axis = [a for a in (0, 1, 2) if a != axis]
        for slice_index in range(size):
            mask = _face_mask(live, data, base, size, axis, sign, u_axis,
                              v_axis, slice_index)
            if mask:
                quads.extend(_merge_mask(mask, base, size, axis, sign, u_axis,
                                         v_axis, slice_index, normal,
                                         voxel_size))
    return ChunkMesh(structure_id=live.structure_id, chunk_index=chunk_index,
                     quads=tuple(quads))


def _face_mask(live: Any, data: Any, base: Tuple[int, int, int], size: int,
               axis: int, sign: int, u_axis: int, v_axis: int,
               slice_index: int) -> Dict[Tuple[int, int], int]:
    """Which cells of this slice have an exposed face, and of what material.

    "Exposed" is decided by asking the *live* structure about the neighbouring
    voxel, including across a chunk boundary. That is why destroying a chunk
    reveals the wall behind it rather than leaving a sealed hole.
    """
    mask: Dict[Tuple[int, int], int] = {}
    for u in range(size):
        for v in range(size):
            coords = [0, 0, 0]
            coords[axis] = base[axis] + slice_index
            coords[u_axis] = base[u_axis] + u
            coords[v_axis] = base[v_axis] + v
            if not live.is_solid(*coords):
                continue
            neighbour = list(coords)
            neighbour[axis] += sign
            if live.is_solid(*neighbour):
                continue
            mask[(u, v)] = data.material_at(*coords)
    return mask


def _merge_mask(mask: Dict[Tuple[int, int], int], base: Tuple[int, int, int],
                size: int, axis: int, sign: int, u_axis: int, v_axis: int,
                slice_index: int, normal: Vec3,
                voxel_size: float) -> List[Quad]:
    """The greedy part: grow each rectangle right, then down, then emit it."""
    quads: List[Quad] = []
    remaining = dict(mask)
    for v in range(size):
        for u in range(size):
            material = remaining.get((u, v))
            if material is None:
                continue
            width = 1
            while (u + width < size
                   and remaining.get((u + width, v)) == material):
                width += 1
            height = 1
            while v + height < size and all(
                    remaining.get((u + i, v + height)) == material
                    for i in range(width)):
                height += 1
            for dv in range(height):
                for du in range(width):
                    remaining.pop((u + du, v + dv), None)
            quads.append(_quad(base, axis, sign, u_axis, v_axis, slice_index,
                               u, v, width, height, material, normal,
                               voxel_size))
    return quads


def _quad(base: Tuple[int, int, int], axis: int, sign: int, u_axis: int,
          v_axis: int, slice_index: int, u: int, v: int, width: int,
          height: int, material: int, normal: Vec3,
          voxel_size: float) -> Quad:
    plane = base[axis] + slice_index + (1 if sign > 0 else 0)

    def corner(du: int, dv: int) -> Vec3:
        point = [0.0, 0.0, 0.0]
        point[axis] = plane * voxel_size
        point[u_axis] = (base[u_axis] + u + du) * voxel_size
        point[v_axis] = (base[v_axis] + v + dv) * voxel_size
        return (point[0], point[1], point[2])

    corners = (corner(0, 0), corner(width, 0), corner(width, height),
               corner(0, height))
    if sign < 0:                       # keep winding consistent per face
        corners = (corners[0], corners[3], corners[2], corners[1])
    return Quad(material=material, normal=normal, corners=corners)


class StructureMesher:
    """Keeps one live structure's chunk meshes up to date.

    `remesh_dirty` drains `LiveStructure.dirty_chunks`, so the work per frame is
    proportional to what was hit, never to how big the keep is. That is the
    entire justification for micro-chunking, and it is one line here.
    """

    def __init__(self, live: Any, *, voxel_size: float = VOXEL_SIZE_M) -> None:
        self.live = live
        self.voxel_size = voxel_size
        self.chunks: Dict[int, ChunkMesh] = {}
        self.remesh_count = 0

    def mesh_all(self) -> Dict[int, ChunkMesh]:
        """Full mesh, for cell load. Skips chunks with no solid voxels."""
        data = self.live.voxel_data
        for index in data.chunk_indices():
            if data.chunk_is_empty(index) or self.live.is_chunk_destroyed(index):
                continue
            self.chunks[index] = mesh_chunk(self.live, index,
                                            voxel_size=self.voxel_size)
            self.remesh_count += 1
        self.live.dirty_chunks.clear()
        return dict(self.chunks)

    def remesh_dirty(self) -> List[int]:
        """Remesh only the chunks that changed. Returns which."""
        touched = self.live.take_dirty()
        for index in touched:
            mesh = mesh_chunk(self.live, index, voxel_size=self.voxel_size)
            if mesh.quads:
                self.chunks[index] = mesh
            else:
                self.chunks.pop(index, None)
            self.remesh_count += 1
        return touched

    def triangle_count(self) -> int:
        return sum(mesh.triangle_count() for mesh in self.chunks.values())

    def nbytes(self) -> int:
        return sum(mesh.nbytes() for mesh in self.chunks.values())

    def report(self) -> Dict[str, Any]:
        return {"structure_id": self.live.structure_id,
                "meshed_chunks": len(self.chunks),
                "triangles": self.triangle_count(),
                "remesh_operations": self.remesh_count,
                "bytes": self.nbytes()}
