"""Terrain. Static, authored, baked once - and structurally separate (L2).

L2: "Terrain and structures are never the same system - dedicated test in 14."

The separation is not a coding-style preference; it is the whole reason the
first of the three founding decisions is affordable:

> Splits one hard problem (general runtime voxel terrain editing + streaming
> remesh) into one *trivial* problem (static terrain) and one small, bounded
> problem (destructible objects at object scale, not world scale).

So this module shares nothing with `lobster.structures`: no base class, no
mesher, no chunk type, no memory pool, and - deliberately - no mutation API at
all. There is no `destroy`, no `set_voxel`, no `remesh`. A terrain object that
cannot be changed cannot grow a runtime-edit path by accident, and section 14
test 1 asserts the two systems never share an object or a pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import AABB, Vec3


class TerrainError(Exception):
    pass


@dataclass(frozen=True)
class TerrainMesh:
    """A cell's terrain, greedy-meshed at build time and never re-meshed.

    Vertices are flat (x, y, z) triples; `indices` are triangle corners;
    `material_slices` maps a palette index to a (start, count) run in `indices`
    so the renderer can draw per material without a second index buffer.
    """

    cell_id: str
    vertices: Tuple[float, ...] = ()
    indices: Tuple[int, ...] = ()
    material_slices: Tuple[Tuple[int, int, int], ...] = ()
    bounds: Optional[AABB] = None

    def world_bounds(self) -> Optional[AABB]:
        """Bounds, derived from the vertices when the bake did not record them.

        A mesh whose `bounds` happen to be missing must not become invisible:
        the culler needs *a* bound, and deriving one from the vertices it
        already has is both cheap and impossible to get wrong. Returns None
        only for a genuinely empty mesh, which has nothing to draw anyway.
        """
        if self.bounds is not None:
            return self.bounds
        if not self.vertices:
            return None
        points = [(self.vertices[i], self.vertices[i + 1], self.vertices[i + 2])
                  for i in range(0, len(self.vertices) - 2, 3)]
        return AABB.from_points(points)

    def triangle_count(self) -> int:
        return len(self.indices) // 3

    def vertex_count(self) -> int:
        return len(self.vertices) // 3

    def nbytes(self) -> int:
        """Declared residency cost: 4-byte floats, 4-byte indices."""
        return len(self.vertices) * 4 + len(self.indices) * 4

    def to_header(self) -> Dict[str, Any]:
        return {"cell_id": self.cell_id,
                "vertex_count": self.vertex_count(),
                "triangle_count": self.triangle_count(),
                "material_slices": [list(s) for s in self.material_slices],
                "bounds": self.bounds.to_dict() if self.bounds else None}


@dataclass(frozen=True)
class TerrainCollider:
    """Static collision for a cell: a baked heightfield on the XZ plane.

    A heightfield rather than the render mesh because terrain is authored flat
    enough for it (Scope 0: "terrain is static/authored"), and because it gives
    the procedural foot-IK in Scope 3 an O(1) ground query instead of a raycast
    against a triangle soup.

    `heights` is row-major, `resolution` samples a side, spanning `size_m`.
    """

    cell_id: str
    resolution: int
    size_m: float
    heights: Tuple[float, ...] = ()
    origin: Vec3 = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if self.resolution <= 0:
            raise TerrainError(
                "{0}: heightfield resolution must be positive".format(self.cell_id))
        expected = self.resolution * self.resolution
        if len(self.heights) != expected:
            raise TerrainError(
                "{0}: heightfield has {1} samples, expected resolution^2 = "
                "{2}".format(self.cell_id, len(self.heights), expected))

    def _sample(self, ix: int, iz: int) -> float:
        n = self.resolution
        ix = 0 if ix < 0 else (n - 1 if ix >= n else ix)
        iz = 0 if iz < 0 else (n - 1 if iz >= n else iz)
        return self.heights[iz * n + ix]

    def covers(self, x: float, z: float) -> bool:
        """Is this column actually over this cell's terrain?

        `ground_height` **clamps** at the edges, which is right for foot IK and
        for the navmesh recompute - a probe a few centimetres past the last
        sample should get the edge height, not a cliff. It is wrong for anything
        asking *which* terrain it is standing on: without this, every resident
        cell answers for every point in the world, and a ray cast at the ground
        in one cell gets claimed by its neighbour.
        """
        span = self.size_m if self.resolution > 1 else 0.0
        return (self.origin[0] <= x <= self.origin[0] + span
                and self.origin[2] <= z <= self.origin[2] + span)

    def ground_height(self, x: float, z: float) -> float:
        """Bilinear ground height at a cell-space point. Clamped at the edges."""
        if self.resolution == 1:
            return self.heights[0] + self.origin[1]
        step = self.size_m / (self.resolution - 1)
        fx = (x - self.origin[0]) / step
        fz = (z - self.origin[2]) / step
        ix, iz = int(fx // 1), int(fz // 1)
        tx, tz = fx - ix, fz - iz
        h00 = self._sample(ix, iz)
        h10 = self._sample(ix + 1, iz)
        h01 = self._sample(ix, iz + 1)
        h11 = self._sample(ix + 1, iz + 1)
        top = h00 + (h10 - h00) * tx
        bottom = h01 + (h11 - h01) * tx
        return self.origin[1] + top + (bottom - top) * tz

    def nbytes(self) -> int:
        return len(self.heights) * 4

    def to_header(self) -> Dict[str, Any]:
        return {"cell_id": self.cell_id, "resolution": self.resolution,
                "size_m": self.size_m, "origin": list(self.origin)}


@dataclass(frozen=True)
class Terrain:
    """A cell's terrain: mesh + collider. Immutable, both halves."""

    cell_id: str
    mesh: TerrainMesh
    collider: TerrainCollider
    lightmap_bytes: int = 0

    def nbytes(self) -> int:
        return self.mesh.nbytes() + self.collider.nbytes()

    def owned_objects(self) -> List[Any]:
        """Every object this system owns, for the L2 separation test."""
        return [self, self.mesh, self.collider]
