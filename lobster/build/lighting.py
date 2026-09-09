"""Per-cell baked lighting (Scope 3).

> Lighting without a GI solution -> **Per-cell baked lightmaps**; structures
> sample ambient at their position. No per-structure light grid to store or
> update on damage.

The second sentence is the design. A structure does not get its own lightmap, so
destroying half a keep does not invalidate anything: the surviving chunks keep
sampling the cell's lightmap at their position, exactly as they did before. That
is the whole reason the runtime has no relighting path and this module lives in
the build step.

The bake is deliberately modest - a sun visibility march plus a sky-openness
term over the terrain column field, one byte per column. It is a lightmap for a
disc-era-budget game with baked-in shadows, not a GI solve, and Scope 0 is
explicit that the point of the tech choices here is to pick the small solved
answer rather than the general one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from ..constants import TERRAIN_VOXEL_SIZE_M
from ..geometry import Vec3, normalize

#: Light reaching a fully shadowed column. Not zero: an unlit voxel that renders
#: pure black reads as a hole in the terrain rather than as shade.
AMBIENT_FLOOR = 0.25

DEFAULT_SUN: Vec3 = (0.4, 0.8, 0.45)


@dataclass(frozen=True)
class Lightmap:
    """One byte of light level per terrain column."""

    cell_id: str
    side: int
    data: bytes
    voxel_size: float = TERRAIN_VOXEL_SIZE_M
    sun: Vec3 = DEFAULT_SUN

    @property
    def dims(self) -> Tuple[int, int, int]:
        return (self.side, 1, self.side)

    def level(self, x: int, z: int) -> float:
        """Light level 0..1 at a column, clamped at the cell edge."""
        if self.side <= 0:
            return 1.0
        x = 0 if x < 0 else (self.side - 1 if x >= self.side else x)
        z = 0 if z < 0 else (self.side - 1 if z >= self.side else z)
        return self.data[z * self.side + x] / 255.0

    def ambient_at(self, position: Vec3) -> float:
        """What a structure samples (Scope 3).

        A position, not a grid: this is the whole per-structure lighting model,
        and it is why destroying a chunk costs no relighting work at all.
        """
        return self.level(int(position[0] // self.voxel_size),
                          int(position[2] // self.voxel_size))

    def nbytes(self) -> int:
        return len(self.data)

    def to_header(self) -> Dict[str, Any]:
        return {"cell_id": self.cell_id, "dims": list(self.dims),
                "voxel_size": self.voxel_size, "sun": list(self.sun)}


def bake_lightmap(cell_id: str, field: Any, *,
                  sun: Vec3 = DEFAULT_SUN,
                  voxel_size: float = TERRAIN_VOXEL_SIZE_M,
                  max_march: int = 48) -> Lightmap:
    """Bake sun visibility and sky openness over a terrain column field."""
    side = field.side
    direction = normalize(sun)
    if direction == (0.0, 0.0, 0.0):
        direction = normalize(DEFAULT_SUN)
    data = bytearray(side * side)
    for z in range(side):
        for x in range(side):
            top = field.height_at(x, z)
            lit = _sun_visible(field, x, z, top, direction, max_march)
            openness = _sky_openness(field, x, z, top)
            level = AMBIENT_FLOOR + (1.0 - AMBIENT_FLOOR) * (
                0.75 * (1.0 if lit else 0.0) + 0.25 * openness)
            data[z * side + x] = max(0, min(255, int(round(level * 255))))
    return Lightmap(cell_id=cell_id, side=side, data=bytes(data),
                    voxel_size=voxel_size, sun=direction)


def _sun_visible(field: Any, x: int, z: int, top: int, direction: Vec3,
                 max_march: int) -> bool:
    """March toward the sun; blocked if any column rises above the ray."""
    if direction[1] <= 0.0:
        return False                        # sun below the horizon
    step_x = direction[0] / direction[1]
    step_z = direction[2] / direction[1]
    for step in range(1, max_march + 1):
        sample_x = int(round(x + step_x * step))
        sample_z = int(round(z + step_z * step))
        if not (0 <= sample_x < field.side and 0 <= sample_z < field.side):
            return True                     # left the cell without hitting
        if field.height_at(sample_x, sample_z) > top + step:
            return False
    return True


def _sky_openness(field: Any, x: int, z: int, top: int) -> float:
    """How much of the immediate horizon is below this column.

    A cheap ambient-occlusion stand-in: a column at the bottom of a pit is
    darker than one on a ridge, without anything resembling a GI solve.
    """
    open_count = 0
    total = 0
    for dx in (-2, -1, 0, 1, 2):
        for dz in (-2, -1, 0, 1, 2):
            if dx == 0 and dz == 0:
                continue
            total += 1
            if field.height_at(x + dx, z + dz) <= top:
                open_count += 1
    return open_count / total if total else 1.0


def lighting_report(lightmap: Lightmap) -> Dict[str, Any]:
    if not lightmap.data:
        return {"cell_id": lightmap.cell_id, "columns": 0}
    values = list(lightmap.data)
    return {"cell_id": lightmap.cell_id,
            "columns": len(values),
            "bytes": lightmap.nbytes(),
            "min_level": min(values) / 255.0,
            "max_level": max(values) / 255.0,
            "mean_level": sum(values) / len(values) / 255.0}
