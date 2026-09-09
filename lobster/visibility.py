"""Cell-boundary visibility and the draw list (Scope 1, 3).

> | Visibility / overdraw | **Cell-boundary culling** | Only resident cells draw. |

This is the "what should be drawn, from where, in what order" half of a
renderer, and it is entirely geometry - so it lives here rather than inside any
particular viewer, and a software rasteriser, a GPU renderer and a test all
consume the same answer.

**Lobster does not invent where a cell is; content declares it.** A connection
carries where an arrival *lands*, not where the neighbouring cell *sits* - so
until D21 there was no cell-to-cell offset anywhere in Lobster or Octopus, and a
renderer could only ever draw one cell. Exterior cells now author
`Location.exterior_grid` and `lobster.cell.cell_placement` turns that into a
world offset; interiors have none, because an interior *is* its own coordinate
space.

`build_draw_list` still invents nothing: it takes each cell **with the placement
it is handed**, and `CellManager.placements` is what reads them off the records.
Hand it one cell and one cell draws, which stays right for interiors. Hand it a
placed exterior ring and the world is continuous across the boundary.

L4 holds: this reports what is visible. It does not decide what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Tuple)

from .camera import Camera
from .geometry import AABB, Transform, Vec3, distance
from .tiers import DORMANT

#: What a draw item stands for. Deliberately coarse: a renderer needs to know
#: which pool a thing came from (terrain and structures are separate systems,
#: L2) and nothing else about what it means.
#: Bounding radius for a placed item in the draw list. Items declare no extent
#: (`Item.model_ref` resolves to nothing until there is an asset pipeline), so
#: this is invented - and it is a *draw* bound, deliberately larger than
#: `selection.ITEM_PICK_RADIUS_M`: culling something that turns out to be
#: invisible costs one wasted draw, while culling something visible is a
#: missing sword. A cull must err towards drawing.
ITEM_DRAW_RADIUS_M = 0.6

TERRAIN = "terrain"
STRUCTURE = "structure"
ENTITY = "entity"
PROP = "prop"
ITEM = "item"

DRAW_KINDS: Tuple[str, ...] = (TERRAIN, STRUCTURE, ENTITY, PROP, ITEM)


@dataclass(frozen=True)
class DrawItem:
    """One thing to draw, and where it is."""

    kind: str
    cell_id: str
    item_id: str
    #: placement of the cell this item belongs to, in world space
    cell_placement: Transform
    #: bounding sphere in world space, for sorting and for the viewer's own
    #: coarse rejection
    center: Vec3
    radius: float
    distance: float

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "cell_id": self.cell_id,
                "item_id": self.item_id, "center": list(self.center),
                "radius": self.radius, "distance": self.distance}


@dataclass
class VisibilityStats:
    """What culling actually saved. Measurable, like every other budget."""

    cells_considered: int = 0
    cells_drawn: int = 0
    items_considered: int = 0
    items_drawn: int = 0

    def culled(self) -> int:
        return self.items_considered - self.items_drawn

    def to_dict(self) -> Dict[str, Any]:
        return {"cells_considered": self.cells_considered,
                "cells_drawn": self.cells_drawn,
                "items_considered": self.items_considered,
                "items_drawn": self.items_drawn, "culled": self.culled()}


@dataclass(frozen=True)
class DrawList:
    """Everything visible this frame, nearest first."""

    camera: Camera
    items: Tuple[DrawItem, ...] = ()
    stats: VisibilityStats = dc_field(default_factory=VisibilityStats)

    def of_kind(self, kind: str) -> List[DrawItem]:
        return [i for i in self.items if i.kind == kind]

    def cells(self) -> List[str]:
        seen: Dict[str, None] = {}
        for item in self.items:
            seen.setdefault(item.cell_id)
        return list(seen)

    def to_dict(self) -> Dict[str, Any]:
        return {"camera": self.camera.to_dict(),
                "items": [i.to_dict() for i in self.items],
                "stats": self.stats.to_dict()}


def _sphere_of(box: AABB) -> Tuple[Vec3, float]:
    center = box.center()
    return center, distance(center, box.maximum)


def _placed(box: AABB, placement: Transform) -> AABB:
    """A cell-space box in world space, re-bounded axis-aligned."""
    lo, hi = box.minimum, box.maximum
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
               for z in (lo[2], hi[2])]
    return AABB.from_points(placement.apply(c) for c in corners)


def build_draw_list(camera: Camera,
                    cells: Iterable[Any],
                    *,
                    placements: Optional[Dict[str, Transform]] = None,
                    include_dormant_entities: bool = False,
                    view: Any = None) -> DrawList:
    """Cull a set of resident cells against the camera.

    `cells` are `ResidentCell`s. `placements` maps a cell id to where that cell
    sits in world space; a cell with no placement is treated as being at the
    origin, which is right when you are drawing one cell and is the caller's
    problem to fix when you are drawing several (see the module docstring).

    Entities at DORMANT tier are skipped by default. A DORMANT entity has no
    rig registered - that is what the tier means (CONTRACT §3) - so there is
    nothing to draw and asking for one would raise. A caller that draws
    billboards or markers for them passes `include_dormant_entities=True` and
    gets the entries without rigs.
    """
    placements = placements or {}
    stats = VisibilityStats()
    items: List[DrawItem] = []

    for cell in cells:
        stats.cells_considered += 1
        placement = placements.get(cell.cell_id, Transform())
        drew_any = False

        # -- terrain (its own pool; L2) -------------------------------------
        terrain = getattr(cell, "terrain", None)
        terrain_bounds = (terrain.mesh.world_bounds()
                          if terrain is not None else None)
        if terrain_bounds is not None:
            stats.items_considered += 1
            box = _placed(terrain_bounds, placement)
            center, radius = _sphere_of(box)
            if camera.sees_sphere(center, radius):
                items.append(DrawItem(
                    kind=TERRAIN, cell_id=cell.cell_id,
                    item_id=cell.cell_id, cell_placement=placement,
                    center=center, radius=radius,
                    distance=camera.distance_to(center)))
                drew_any = True

        # -- structures (the other pool) -------------------------------------
        for structure_id, live in sorted(getattr(cell, "structures", {}).items()):
            stats.items_considered += 1
            box = _placed(live.voxel_data.aabb_world(), placement)
            center, radius = _sphere_of(box)
            if not camera.sees_sphere(center, radius):
                continue
            items.append(DrawItem(
                kind=STRUCTURE, cell_id=cell.cell_id, item_id=structure_id,
                cell_placement=placement, center=center, radius=radius,
                distance=camera.distance_to(center)))
            drew_any = True

        # -- props ------------------------------------------------------------
        for prop in getattr(getattr(cell, "bundle", None), "props", ()) or ():
            stats.items_considered += 1
            center = placement.apply(prop.transform.position)
            radius = 1.0                     # props declare no extent yet
            if not camera.sees_sphere(center, radius):
                continue
            items.append(DrawItem(
                kind=PROP, cell_id=cell.cell_id, item_id=prop.prop_id,
                cell_placement=placement, center=center, radius=radius,
                distance=camera.distance_to(center)))
            drew_any = True

        # -- placed items (Scope 8) --------------------------------------------
        # Same shape as props and deliberately a separate kind: a prop is
        # decoration baked into the bundle, an item is an Octopus record with
        # an id worth reporting (D36). Neither declares an extent - there is no
        # asset pipeline, so `model_ref` resolves to nothing - and the radius
        # here is the same invented number selection uses, kept in one place.
        if view is not None:
            for record in view.items_in_location(cell.cell_id):
                raw = (record.get("world_transform") or {}).get("position")
                if not raw:
                    continue
                stats.items_considered += 1
                center = placement.apply((float(raw[0]), float(raw[1]),
                                          float(raw[2])))
                if not camera.sees_sphere(center, ITEM_DRAW_RADIUS_M):
                    continue
                items.append(DrawItem(
                    kind=ITEM, cell_id=cell.cell_id, item_id=record["id"],
                    cell_placement=placement, center=center,
                    radius=ITEM_DRAW_RADIUS_M,
                    distance=camera.distance_to(center)))
                drew_any = True

        # -- entities ----------------------------------------------------------
        index = getattr(cell, "index", None)
        skeletons = getattr(cell, "skeletons", {})
        if index is not None:
            for entry in index.entries():
                if entry.tier == DORMANT and not include_dormant_entities:
                    continue
                stats.items_considered += 1
                skeleton = skeletons.get(entry.entity_id)
                radius = (skeleton.bound_radius() if skeleton is not None
                          else 1.0)
                center = placement.apply(entry.position)
                if not camera.sees_sphere(center, radius):
                    continue
                items.append(DrawItem(
                    kind=ENTITY, cell_id=cell.cell_id,
                    item_id=entry.entity_id, cell_placement=placement,
                    center=center, radius=radius,
                    distance=camera.distance_to(center)))
                drew_any = True

        if drew_any:
            stats.cells_drawn += 1

    # Nearest first: a z-buffered opaque pass rejects the most pixels that way,
    # and a viewer that wants painter's order can reverse it.
    items.sort(key=lambda i: (i.distance, i.kind, i.item_id))
    stats.items_drawn = len(items)
    return DrawList(camera=camera, items=tuple(items), stats=stats)
