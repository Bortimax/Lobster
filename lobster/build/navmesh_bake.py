"""Baking a cell's navmesh (Scope 3, 10).

> Navigation, common case -> **Per-cell baked navmesh**. No runtime regen for
> static terrain.

The bake walks the terrain column field, marks which columns an agent can stand
on, greedy-merges them into convex rectangles, and links rectangles that share
an edge and are within one step of each other. Convex-and-rectangular is not a
simplification for its own sake: it keeps point-in-polygon cheap at runtime and
makes "walk from this polygon to that one" a straight line, which is all the
leash movement in Scope 9 ever asks for.

Portals come from the manifest, not from geometry. Scope 4: "the connection owns
the spawn point", so the polygon containing a connection's spawn transform is
that connection's door, and `NavPoly.connection_targets` records it - plural,
because one merged polygon commonly holds several. That is what
lets `lobster.connection_graph` ask "can an agent still reach the door to
`cell-field`" after a collapse - and, just as importantly, tell the difference
between "cannot reach it" and "this cell bakes no door there at all".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..constants import TERRAIN_VOXEL_SIZE_M
from ..geometry import Vec3
from ..constants import VOXEL_SIZE_M
from ..navmesh import (DEFAULT_AGENT_HEIGHT_M, NavPoly, Navmesh,
                       clearance_blocked)

#: How much vertical difference an agent can walk up between adjacent polys.
DEFAULT_MAX_STEP_M = 0.6

#: Steepest walkable gradient, as a height difference between adjacent columns.
DEFAULT_MAX_SLOPE_M = 1.0


@dataclass(frozen=True)
class BakeSettings:
    """Per-cell navmesh bake parameters, authored in the manifest."""

    agent_height: float = DEFAULT_AGENT_HEIGHT_M
    max_step: float = DEFAULT_MAX_STEP_M
    max_slope: float = DEFAULT_MAX_SLOPE_M
    voxel_size: float = TERRAIN_VOXEL_SIZE_M

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "BakeSettings":
        raw = raw or {}
        return cls(agent_height=float(raw.get("agent_height",
                                              DEFAULT_AGENT_HEIGHT_M)),
                   max_step=float(raw.get("max_step", DEFAULT_MAX_STEP_M)),
                   max_slope=float(raw.get("max_slope", DEFAULT_MAX_SLOPE_M)),
                   voxel_size=float(raw.get("voxel_size",
                                            TERRAIN_VOXEL_SIZE_M)))

    def to_dict(self) -> Dict[str, Any]:
        return {"agent_height": self.agent_height, "max_step": self.max_step,
                "max_slope": self.max_slope, "voxel_size": self.voxel_size}


def bake_navmesh(cell_id: str, field: Any, *,
                 settings: Optional[BakeSettings] = None,
                 portals: Optional[Mapping[str, Vec3]] = None,
                 structures: Sequence[Any] = ()) -> Navmesh:
    """Bake a cell's walkable surface into convex polygons.

    `portals` maps a target location id to the spawn point of the connection
    leading there, in cell space. Whichever polygon contains that point becomes
    the door.

    **`structures` are the cell's authored, intact structures, and leaving them
    out was a hole rather than a simplification** (review L2). The bake ran on
    the terrain heightfield alone; structures were then used only to infer
    which existing polygons a *destruction* might affect. So a pristine wall
    corrected nothing, and an agent walked into a solid gatehouse: the build
    reported success, and the polygon under the wall was as walkable as the
    field around it.

    They are masked out **before** the greedy merge, which is what keeps the
    subdivision honest. Marking whole polygons unwalkable afterwards would be
    far worse than ignoring structures: a flat field merges into a *single*
    rectangle, so one cube in the middle of it would make the entire cell
    impassable. Removing the occupied columns first makes the mesher cut
    rectangles around the footprint, which is the subdivision the review asked
    for and costs nothing extra.
    """
    settings = settings or BakeSettings()
    walkable = _walkable_columns(field, settings)
    _mask_structures(field, walkable, structures, settings)
    rectangles = _merge_rectangles(field, walkable)
    polys = _to_polys(field, rectangles, settings)
    _link_neighbours(polys, rectangles, settings)
    navmesh = Navmesh(cell_id, polys)
    return _attach_portals(navmesh, portals or {})


def _walkable_columns(field: Any, settings: BakeSettings) -> List[List[bool]]:
    """A column is walkable when it exists and its neighbours are not a cliff."""
    side = field.side
    out = [[False] * side for _ in range(side)]
    for x in range(side):
        for z in range(side):
            height = field.height_at(x, z)
            if height == 0:
                continue
            steepest = 0.0
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = x + dx, z + dz
                if not (0 <= nx < side and 0 <= nz < side):
                    continue
                delta = abs(field.height_at(nx, nz) - height) * settings.voxel_size
                steepest = max(steepest, delta)
            out[x][z] = steepest <= settings.max_slope
    return out


def _mask_structures(field: Any, walkable: List[List[bool]],
                     structures: Sequence[Any],
                     settings: BakeSettings) -> None:
    """Clear every column an intact structure is standing in.

    Asked at the column's own surface with the same `clearance_blocked` the
    runtime patch uses, so a cell that is never damaged and a cell that is
    damaged and recomputed cannot disagree about what "walkable" meant.

    Note the two voxel sizes: columns are terrain-sized (`settings.voxel_size`)
    and structures are finer (`VOXEL_SIZE_M`), so the probe walks the agent's
    headroom in *structure* voxels while the grid it clears is terrain's.
    """
    structures = list(structures)
    if not structures:
        return
    v = settings.voxel_size
    for x in range(field.side):
        for z in range(field.side):
            if not walkable[x][z]:
                continue
            surface = field.height_at(x, z) * v
            if clearance_blocked((x + 0.5) * v, surface, (z + 0.5) * v,
                                 structures, settings.agent_height,
                                 VOXEL_SIZE_M):
                walkable[x][z] = False


def _merge_rectangles(field: Any, walkable: List[List[bool]]
                      ) -> List[Tuple[int, int, int, int, int]]:
    """Greedy-merge walkable columns of equal height into (x, z, w, d, height)."""
    side = field.side
    claimed = [[False] * side for _ in range(side)]
    out: List[Tuple[int, int, int, int, int]] = []
    for z in range(side):
        for x in range(side):
            if claimed[x][z] or not walkable[x][z]:
                continue
            height = field.height_at(x, z)
            width = 1
            while (x + width < side and walkable[x + width][z]
                   and not claimed[x + width][z]
                   and field.height_at(x + width, z) == height):
                width += 1
            depth = 1
            while z + depth < side and all(
                    walkable[x + i][z + depth] and not claimed[x + i][z + depth]
                    and field.height_at(x + i, z + depth) == height
                    for i in range(width)):
                depth += 1
            for dz in range(depth):
                for dx in range(width):
                    claimed[x + dx][z + dz] = True
            out.append((x, z, width, depth, height))
    return out


def _to_polys(field: Any, rectangles: Sequence[Tuple[int, int, int, int, int]],
              settings: BakeSettings) -> List[NavPoly]:
    polys: List[NavPoly] = []
    v = settings.voxel_size
    for index, (x, z, width, depth, height) in enumerate(rectangles):
        x0, x1 = x * v, (x + width) * v
        z0, z1 = z * v, (z + depth) * v
        polys.append(NavPoly(poly_id=index,
                             points=((x0, z0), (x1, z0), (x1, z1), (x0, z1)),
                             y=height * v))
    return polys


def _link_neighbours(polys: List[NavPoly],
                     rectangles: Sequence[Tuple[int, int, int, int, int]],
                     settings: BakeSettings) -> None:
    """Link rectangles that share an edge and are within one step vertically."""
    links: Dict[int, List[int]] = {p.poly_id: [] for p in polys}
    for i, (xi, zi, wi, di, hi) in enumerate(rectangles):
        for j, (xj, zj, wj, dj, hj) in enumerate(rectangles):
            if i >= j:
                continue
            if abs(hi - hj) * settings.voxel_size > settings.max_step:
                continue
            touch_x = (xi + wi == xj or xj + wj == xi) and not (
                zi + di <= zj or zj + dj <= zi)
            touch_z = (zi + di == zj or zj + dj == zi) and not (
                xi + wi <= xj or xj + wj <= xi)
            if touch_x or touch_z:
                links[i].append(j)
                links[j].append(i)
    for index, poly in enumerate(polys):
        polys[index] = NavPoly(poly_id=poly.poly_id, points=poly.points,
                               y=poly.y,
                               neighbours=tuple(sorted(links[poly.poly_id])),
                               connection_targets=poly.connection_targets)


def _attach_portals(navmesh: Navmesh,
                    portals: Mapping[str, Vec3]) -> Navmesh:
    """Mark the polygon under each connection's spawn point as its door.

    **Adds; it does not replace.** Two doors on one polygon is the ordinary
    case rather than the exotic one - greedy meshing merges a flat room into a
    single rectangle, so a room with two exits has both spawn points inside the
    same polygon. Overwriting meant the second door erased the first, which
    then lay on the navmesh looking walkable and answered "no opinion" to every
    connection question asked about it (review L4).
    """
    for target, point in sorted(portals.items()):
        poly_id = navmesh.poly_at(point)
        if poly_id is None:
            continue
        poly = navmesh.polys[poly_id]
        if target in poly.connection_targets:
            continue
        navmesh.polys[poly_id] = NavPoly(
            poly_id=poly.poly_id, points=poly.points, y=poly.y,
            neighbours=poly.neighbours,
            connection_targets=tuple(sorted(
                poly.connection_targets + (target,))))
    return navmesh


def unreachable_portals(navmesh: Navmesh,
                        portals: Mapping[str, Vec3]) -> List[Dict[str, Any]]:
    """Build-step findings for doors that landed off the navmesh.

    A spawn point outside every walkable polygon means an arrival stands
    somewhere nobody can walk out of. Reporting it at build time is the whole
    point of Scope 15.2 - "the build step should say so loudly" - and it is
    cheap here and invisible at runtime.
    """
    findings: List[Dict[str, Any]] = []
    for target, point in sorted(portals.items()):
        if navmesh.poly_at(point) is None:
            findings.append({
                "code": "portal_off_navmesh",
                "detail": "the spawn point for the connection to {0!r} at {1} "
                          "is not on any walkable polygon; an arrival there "
                          "would have nowhere to walk".format(
                              target, list(point))})
    return findings


def bake_report(navmesh: Navmesh) -> Dict[str, Any]:
    return {"cell_id": navmesh.cell_id, "polys": len(navmesh.polys),
            "portals": sorted({t for p in navmesh.polys.values()
                               for t in p.connection_targets}),
            "links": sum(len(p.neighbours) for p in navmesh.polys.values()) // 2}
