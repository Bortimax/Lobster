"""Build-time terrain meshing (Scope 3, L2).

> Turning a voxel grid into a mesh without a triangle explosion -> **Greedy
> meshing**. **Build-time for terrain**; touched-micro-chunk-only at runtime for
> structures.

This shares nothing with `lobster.structure_mesher` - no helper, no quad type,
no import, no base class. Scope 15.1 ("Don't let terrain and structures share a
code path") and L2 both require that, and DECISIONS.md D11 records the cost.

It turns out not to be much of a cost, because the two are not really the same
problem once you look at them:

* A structure is a **volume** that changes. It is meshed one 8^3 chunk at a
  time, inside a frame, against solidity masked by break-state, and the chunk
  boundary matters because destroying a chunk exposes its neighbours.
* Terrain is a **surface** that never changes. It is meshed once, offline,
  whole-cell, and it is authored as a landscape: one solid column per (x, z),
  with a top face and some cliff faces where neighbouring columns differ.

So this mesher is column-oriented rather than voxel-oriented. It walks the
column grid once, greedy-merges the top surface into rectangles of constant
height and material, and emits a skirt where a column's neighbour sits lower.
Cost is O(columns), not O(voxels), which is what makes a 128 m cell a build step
rather than a coffee break.

It also produces the heightfield collider from the same pass, because the top of
each column is exactly what a foot lands on (Scope 3's procedural IK wants an
O(1) ground query, not a raycast into a triangle soup).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..constants import EXTERIOR_CELL_SIZE_M, TERRAIN_VOXEL_SIZE_M
from ..geometry import AABB
from ..terrain import Terrain, TerrainCollider, TerrainError, TerrainMesh


@dataclass(frozen=True)
class ColumnField:
    """A cell's terrain as one column per (x, z): its top voxel and material.

    `height[x][z]` is the number of solid voxels in that column (0 for a hole),
    `material[x][z]` the palette index of its top voxel. This is the whole input
    the mesher needs, and building it from a `.vox` model is a single pass.
    """

    side: int
    heights: Tuple[Tuple[int, ...], ...]
    materials: Tuple[Tuple[int, ...], ...]

    def height_at(self, x: int, z: int) -> int:
        if 0 <= x < self.side and 0 <= z < self.side:
            return self.heights[x][z]
        return 0

    def material_at(self, x: int, z: int) -> int:
        return self.materials[x][z]


def column_field_from_vox(model: Any, *, side: Optional[int] = None
                          ) -> ColumnField:
    """Collapse a `.vox` model into columns.

    A landscape authored in MagicaVoxel is a heightfield with paint on top;
    anything overhanging it (an arch, a cave roof) is a *structure*, not terrain,
    because terrain is static and structures are what get destroyed. Collapsing
    to columns states that boundary rather than half-supporting overhangs and
    leaving an author to discover which parts of their cliff are destructible.
    """
    extent = side or max(model.size[0], model.size[2], 1)
    heights = [[0] * extent for _ in range(extent)]
    materials = [[0] * extent for _ in range(extent)]
    for (x, y, z), colour in model.voxels.items():
        if x >= extent or z >= extent:
            continue
        if y + 1 > heights[x][z]:
            heights[x][z] = y + 1
            materials[x][z] = colour
    return ColumnField(side=extent,
                       heights=tuple(tuple(row) for row in heights),
                       materials=tuple(tuple(row) for row in materials))


def flat_column_field(side: int, *, height: int = 1,
                      material: int = 1) -> ColumnField:
    """A flat cell. Useful for a cell that is only a floor, and for tests."""
    return ColumnField(
        side=side,
        heights=tuple(tuple(height for _ in range(side)) for _ in range(side)),
        materials=tuple(tuple(material for _ in range(side))
                        for _ in range(side)))


class _MeshBuilder:
    """Accumulates vertices and indices, grouped by material.

    Grouping as it goes means `material_slices` comes out contiguous, so the
    renderer draws per material off one index buffer - which is the point of
    carrying a palette index per voxel instead of a colour (Scope 6).
    """

    def __init__(self) -> None:
        self.by_material: Dict[int, List[Tuple[float, ...]]] = {}

    def quad(self, material: int, a, b, c, d) -> None:
        self.by_material.setdefault(material, []).append((a, b, c, d))

    def finish(self, cell_id: str) -> TerrainMesh:
        vertices: List[float] = []
        indices: List[int] = []
        slices: List[Tuple[int, int, int]] = []
        points: Dict[Tuple[float, float, float], int] = {}

        def index_of(point) -> int:
            key = (float(point[0]), float(point[1]), float(point[2]))
            existing = points.get(key)
            if existing is not None:
                return existing
            new = len(points)
            points[key] = new
            vertices.extend(key)
            return new

        for material in sorted(self.by_material):
            start = len(indices)
            for a, b, c, d in self.by_material[material]:
                ia, ib, ic, id_ = (index_of(a), index_of(b), index_of(c),
                                   index_of(d))
                indices.extend((ia, ib, ic, ia, ic, id_))
            slices.append((material, start, len(indices) - start))

        bounds = None
        if vertices:
            points_list = [tuple(vertices[i:i + 3])
                           for i in range(0, len(vertices), 3)]
            bounds = AABB.from_points(points_list)
        return TerrainMesh(cell_id=cell_id, vertices=tuple(vertices),
                           indices=tuple(indices),
                           material_slices=tuple(slices), bounds=bounds)


def mesh_terrain(cell_id: str, field: ColumnField, *,
                 voxel_size: float = TERRAIN_VOXEL_SIZE_M,
                 cell_size_m: float = EXTERIOR_CELL_SIZE_M) -> Terrain:
    """Mesh a cell's terrain and derive its collider, in one pass."""
    if field.side <= 0:
        raise TerrainError("cell {0!r}: terrain has no columns".format(cell_id))
    builder = _MeshBuilder()
    _mesh_top_surface(builder, field, voxel_size)
    _mesh_cliffs(builder, field, voxel_size)
    mesh = builder.finish(cell_id)

    heights = []
    for z in range(field.side):
        for x in range(field.side):
            heights.append(field.height_at(x, z) * voxel_size)
    collider = TerrainCollider(
        cell_id=cell_id, resolution=field.side,
        size_m=(field.side - 1) * voxel_size if field.side > 1 else cell_size_m,
        heights=tuple(heights))
    return Terrain(cell_id=cell_id, mesh=mesh, collider=collider)


def _mesh_top_surface(builder: _MeshBuilder, field: ColumnField,
                      voxel_size: float) -> None:
    """Greedy-merge the top faces: grow east, then south, then emit."""
    claimed = [[False] * field.side for _ in range(field.side)]
    for z in range(field.side):
        for x in range(field.side):
            if claimed[x][z] or field.height_at(x, z) == 0:
                continue
            height = field.height_at(x, z)
            material = field.material_at(x, z)

            width = 1
            while (x + width < field.side and not claimed[x + width][z]
                   and field.height_at(x + width, z) == height
                   and field.material_at(x + width, z) == material):
                width += 1
            depth = 1
            while z + depth < field.side and all(
                    not claimed[x + i][z + depth]
                    and field.height_at(x + i, z + depth) == height
                    and field.material_at(x + i, z + depth) == material
                    for i in range(width)):
                depth += 1
            for dz in range(depth):
                for dx in range(width):
                    claimed[x + dx][z + dz] = True

            y = height * voxel_size
            x0, x1 = x * voxel_size, (x + width) * voxel_size
            z0, z1 = z * voxel_size, (z + depth) * voxel_size
            # Wound so the face normal points **up**. The obvious corner order
            # (x0z0, x1z0, x1z1, x0z1) points it straight down into the ground,
            # which no test noticed until something tried to light the mesh:
            # the whole surface shaded as if it faced away from the sun, and
            # backface culling in any real renderer would have discarded it.
            builder.quad(material, (x0, y, z0), (x0, y, z1), (x1, y, z1),
                         (x1, y, z0))


def _mesh_cliffs(builder: _MeshBuilder, field: ColumnField,
                 voxel_size: float) -> None:
    """The vertical face wherever a column stands taller than its neighbour.

    Merged along the shared edge, which is where a terrace or a cliff actually
    runs; merging vertically as well would need a second pass for a handful of
    triangles on a landscape that is mostly flat.
    """
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for dx, dz in directions:
        claimed = [[False] * field.side for _ in range(field.side)]
        for z in range(field.side):
            for x in range(field.side):
                if claimed[x][z]:
                    continue
                here = field.height_at(x, z)
                there = field.height_at(x + dx, z + dz)
                if here <= there:
                    continue
                material = field.material_at(x, z)
                run = 1
                # extend along the edge, perpendicular to the face normal
                step = (0, 1) if dx else (1, 0)
                while True:
                    nx = x + step[0] * run
                    nz = z + step[1] * run
                    if not (0 <= nx < field.side and 0 <= nz < field.side):
                        break
                    if claimed[nx][nz]:
                        break
                    if (field.height_at(nx, nz) != here
                            or field.height_at(nx + dx, nz + dz) != there
                            or field.material_at(nx, nz) != material):
                        break
                    run += 1
                for i in range(run):
                    claimed[x + step[0] * i][z + step[1] * i] = True
                _emit_cliff(builder, x, z, dx, dz, step, run, here, there,
                            material, voxel_size)


def _emit_cliff(builder: _MeshBuilder, x: int, z: int, dx: int, dz: int,
                step: Tuple[int, int], run: int, here: int, there: int,
                material: int, voxel_size: float) -> None:
    top = here * voxel_size
    bottom = there * voxel_size
    if dx:
        plane = (x + (1 if dx > 0 else 0)) * voxel_size
        a0 = z * voxel_size
        a1 = (z + run) * voxel_size
        corners = [(plane, bottom, a0), (plane, top, a0),
                   (plane, top, a1), (plane, bottom, a1)]
    else:
        plane = (z + (1 if dz > 0 else 0)) * voxel_size
        a0 = x * voxel_size
        a1 = (x + run) * voxel_size
        corners = [(a0, bottom, plane), (a1, bottom, plane),
                   (a1, top, plane), (a0, top, plane)]
    # These windings give an outward normal for the +X and +Z faces; the
    # opposite faces need the reverse, or half the cliffs in a cell point into
    # the hill they belong to.
    if dx < 0 or dz < 0:
        corners.reverse()
    builder.quad(material, *corners)


def terrain_report(terrain: Terrain, field: ColumnField) -> Dict[str, Any]:
    return {"cell_id": terrain.cell_id,
            "columns": field.side * field.side,
            "vertices": terrain.mesh.vertex_count(),
            "triangles": terrain.mesh.triangle_count(),
            "mesh_bytes": terrain.mesh.nbytes(),
            "collider_bytes": terrain.collider.nbytes()}
