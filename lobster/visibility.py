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

from .budgets import BudgetViolation
from .camera import Camera
from .conformance import PLACE_BATCH
from .constants import (MAX_VISIBLE_PLACEMENTS_PER_FRAME,
                        MODEL_DRAW_BUDGET_US, PER_PLACEMENT_US)
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
    #: which library model draws this, or "" for something with no mesh - an
    #: entity, terrain, or a prop whose author gave it none. An empty ref is
    #: the impostor, and that is a documented state rather than a fault.
    model_ref: str = ""
    #: this object's own placement *within its cell*. World placement is
    #: `cell_placement` composed with it, in that order - the same two-step
    #: `_draw_structure` already does for a structure origin.
    transform: Optional[Transform] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "cell_id": self.cell_id,
                "item_id": self.item_id, "center": list(self.center),
                "radius": self.radius, "distance": self.distance,
                "model_ref": self.model_ref}


def modelled_placement_cost_us(placements: int) -> float:
    """What this frame's model assembly cost, from a counter alone.

    Deterministic and machine-independent, exactly like
    `constants.modelled_cost_us`: the budget is a statement about a measured
    per-unit cost times a count, not about a wall clock that varies with what
    else the machine is doing.
    """
    return placements * PER_PLACEMENT_US


@dataclass
class VisibilityStats:
    """What culling actually saved. Measurable, like every other budget."""

    cells_considered: int = 0
    cells_drawn: int = 0
    items_considered: int = 0
    items_drawn: int = 0
    #: visible props and items with a model - the unit the §6 budget is in.
    #: Props and items together, because they cost the same and go through the
    #: same path; splitting them would budget an implementation detail.
    placements_drawn: int = 0

    def culled(self) -> int:
        return self.items_considered - self.items_drawn

    def placement_cost_us(self) -> float:
        return modelled_placement_cost_us(self.placements_drawn)

    def to_dict(self) -> Dict[str, Any]:
        return {"cells_considered": self.cells_considered,
                "cells_drawn": self.cells_drawn,
                "items_considered": self.items_considered,
                "items_drawn": self.items_drawn, "culled": self.culled(),
                "placements_drawn": self.placements_drawn,
                "placement_cost_us": self.placement_cost_us()}


@dataclass(frozen=True)
class DrawList:
    """Everything visible this frame, nearest first."""

    camera: Camera
    items: Tuple[DrawItem, ...] = ()
    stats: VisibilityStats = dc_field(default_factory=VisibilityStats)
    #: `(cell_id, model_ref)` -> the instance bytes for that group, already
    #: packed by the seam kernel that did the culling (D53). The GPU backend
    #: uploads these directly; a draw list built without a library has none and
    #: the backend packs per item as it always did. Not part of `to_dict`: it
    #: is a buffer, not a description.
    instances: Dict[Tuple[str, str], bytes] = dc_field(default_factory=dict)

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


def _has_mesh(library: Any, model_ref: str) -> bool:
    """Would this model ever be drawn as geometry?

    The packed instances exist for the GPU backend, and it draws from buffers
    residency uploaded - so a blob for a model the library cannot satisfy can
    never be used, and packing one is work with no consumer. An empty mesh
    counts as unresolvable for the same reason `_mesh_for` treats it that way.
    """
    if not model_ref or library is None:
        return False
    mesh = library.models.get(model_ref)
    return mesh is not None and not mesh.is_empty()


def _place_payload(camera: Camera, placement: Transform, radius: float,
                   flat: Sequence[float], lightmap: Any) -> Dict[str, Any]:
    """One `place_batch` call's input.

    The camera goes over as its *fields* rather than its derived basis, so an
    implementation has to reproduce the orthonormalisation rather than be
    handed it - which is the derivation most likely to differ, and therefore
    the one the differential suite most needs to see.
    """
    return {
        "camera": {"position": camera.position, "forward": camera.forward,
                   "up": camera.up, "fov_y_deg": camera.fov_y_deg,
                   "aspect": camera.aspect, "near": camera.near,
                   "far": camera.far},
        "cell": {"position": placement.position,
                 "rotation": placement.rotation},
        "radius": radius,
        "placements": flat,
        "lightmap": lightmap,
    }


def _model_radius(library: Any, model_ref: str,
                  fallback: float) -> Tuple[float, str]:
    """How big this thing actually is, and whether a model said so.

    The fallback numbers below this call are invented - `1.0` for a prop,
    `ITEM_DRAW_RADIUS_M` for an item - and were invented precisely because
    nothing knew a prop's extent. A model does, so when there is one the guess
    is not used. That matters as a *cull* and not as trivia: a 3 m statue culled
    against a 1 m guess pops out of view while it is still on screen, and "a
    cull must err towards drawing" is the rule this file already states.
    """
    if not model_ref or library is None:
        return fallback, model_ref
    mesh = library.models.get(model_ref)
    if mesh is None or mesh.is_empty():
        # Reported by residency as `missing_models`, not invented around here.
        return fallback, model_ref
    return max(fallback, mesh.bound_radius()), model_ref


def build_draw_list(camera: Camera,
                    cells: Iterable[Any],
                    *,
                    placements: Optional[Dict[str, Transform]] = None,
                    include_dormant_entities: bool = False,
                    library: Any = None,
                    max_visible_placements: int = MAX_VISIBLE_PLACEMENTS_PER_FRAME,
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
    packed: Dict[Tuple[str, str], bytes] = {}
    # Selected once per draw list, not per cell: `select` walks the registry,
    # and doing that nine times a frame would be the dispatch cost this seam
    # exists to avoid. Not cached across calls either - a test that swaps
    # `KERNEL_PREFERENCE` has to be able to.
    from .accel import select as _select_accel
    place = _select_accel()[1][PLACE_BATCH]

    def charge_placement(cell_id: str) -> None:
        """One more thing with a mesh on screen. Raises when the frame is full.

        Counted here and not in a backend because *visible* is decided here,
        and because both backends pay it - the software path walks the mesh,
        the GPU path composes an instance. `0` disables the ceiling, the same
        convention `HitTester` uses.
        """
        stats.placements_drawn += 1
        if (max_visible_placements
                and stats.placements_drawn > max_visible_placements):
            raise BudgetViolation(
                cell_id=cell_id, record_id=None, metric="model_frame_us",
                value=round(stats.placement_cost_us(), 1),
                limit=MODEL_DRAW_BUDGET_US,
                detail="{0} visible placements at {1} us each; the ceiling is "
                       "{2} across every resident cell. Culling is what bounds "
                       "this, so the fix is fewer things on screen at once - "
                       "or a cheaper placement (ASSET_SCOPE 6)".format(
                           stats.placements_drawn, PER_PLACEMENT_US,
                           max_visible_placements))

    for cell in cells:
        stats.cells_considered += 1
        placement = placements.get(cell.cell_id, Transform())
        lightmap = (cell.lightmap_block()
                    if hasattr(cell, "lightmap_block") else None)
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
        # One kernel call per model rather than a Python loop per prop. The
        # rows are built once per residency (`prop_rows`); what crosses the
        # boundary here is a batch, which is the whole of D26's argument.
        rows = cell.prop_rows() if hasattr(cell, "prop_rows") else {}
        for model_ref, (flat, prop_ids) in sorted(rows.items()):
            stats.items_considered += len(prop_ids)
            radius, model_ref = _model_radius(library, model_ref, 1.0)
            result = place(_place_payload(camera, placement, radius, flat,
                                          lightmap))
            for slot, index in enumerate(result["visible"]):
                center = tuple(result["centers"][slot])
                base = index * 7
                items.append(DrawItem(
                    kind=PROP, cell_id=cell.cell_id,
                    item_id=prop_ids[index], cell_placement=placement,
                    center=center, radius=radius,
                    distance=result["distances"][slot],
                    model_ref=model_ref,
                    transform=Transform(
                        position=(flat[base], flat[base + 1], flat[base + 2]),
                        rotation=(flat[base + 3], flat[base + 4],
                                  flat[base + 5], flat[base + 6]))))
                if model_ref:
                    charge_placement(cell.cell_id)
                drew_any = True
            if result["visible"] and _has_mesh(library, model_ref):
                packed[(cell.cell_id, model_ref)] = result["instances"]

        # -- placed items (Scope 8) --------------------------------------------
        # Same shape as props and deliberately a separate kind: a prop is
        # decoration baked into the bundle, an item is an Octopus record with
        # an id worth reporting (D36). `ITEM_DRAW_RADIUS_M` is still the answer
        # for an item whose `model_ref` names nothing the library holds - which
        # after ASSET_SCOPE step 2 is a build error rather than the normal case.
        if view is not None:
            # Items get their rows built per frame rather than cached: they are
            # records that move, and a cache of them needs an invalidation
            # story. Reading seven floats out of a dict is a fraction of what
            # the kernel removes, so this is still most of the win (D53).
            item_rows: Dict[str, Tuple[List[float], List[str]]] = {}
            for record in view.items_in_location(cell.cell_id):
                transform = record.get("world_transform") or {}
                raw = transform.get("position")
                if not raw:
                    continue
                stats.items_considered += 1
                spin = transform.get("rotation") or (0.0, 0.0, 0.0, 1.0)
                flat, ids = item_rows.setdefault(
                    record.get("model_ref") or "", ([], []))
                flat.extend((float(raw[0]), float(raw[1]), float(raw[2]),
                             float(spin[0]), float(spin[1]), float(spin[2]),
                             float(spin[3])))
                ids.append(record["id"])

            for model_ref, (flat, item_ids) in sorted(item_rows.items()):
                radius, model_ref = _model_radius(library, model_ref,
                                                  ITEM_DRAW_RADIUS_M)
                result = place(_place_payload(camera, placement, radius, flat,
                                              lightmap))
                for slot, index in enumerate(result["visible"]):
                    base = index * 7
                    items.append(DrawItem(
                        kind=ITEM, cell_id=cell.cell_id,
                        item_id=item_ids[index], cell_placement=placement,
                        center=tuple(result["centers"][slot]), radius=radius,
                        distance=result["distances"][slot],
                        model_ref=model_ref,
                        transform=Transform(
                            position=(flat[base], flat[base + 1],
                                      flat[base + 2]),
                            rotation=(flat[base + 3], flat[base + 4],
                                      flat[base + 5], flat[base + 6]))))
                    if model_ref:
                        charge_placement(cell.cell_id)
                    drew_any = True
                if result["visible"] and _has_mesh(library, model_ref):
                    key = (cell.cell_id, model_ref)
                    packed[key] = packed.get(key, b"") + result["instances"]

        # -- entities ----------------------------------------------------------
        # An entity is culled **once, as a whole**, and only then does what it
        # wears become draw items. Culling per bone would be finer and wrong:
        # a head visible over a wall has to bring its body, and the rig's own
        # `bound_radius` already covers everything authored to it.
        index = getattr(cell, "index", None)
        skeletons = getattr(cell, "skeletons", {})
        if index is not None:
            #: model ref -> (flat rows, "entity/bone" ids), the same shape the
            #: item pass builds, so worn models go through the same kernel,
            #: the same instance packing and the same placement charge. A
            #: character's arm is a placement exactly as a barrel is.
            worn_rows: Dict[str, Tuple[List[float], List[str]]] = {}
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

                worn = skeleton.worn_models() if skeleton is not None else []
                for bone_id, model_ref, transform in worn:
                    flat, ids = worn_rows.setdefault(model_ref, ([], []))
                    flat.extend((float(transform.position[0]),
                                 float(transform.position[1]),
                                 float(transform.position[2]),
                                 float(transform.rotation[0]),
                                 float(transform.rotation[1]),
                                 float(transform.rotation[2]),
                                 float(transform.rotation[3])))
                    ids.append("{0}/{1}".format(entry.entity_id, bone_id))

                # The impostor item is still emitted for an entity with any
                # *undressed* bone, which is every entity until someone gives
                # it a wardrobe. A fully dressed one does not need it, and
                # emitting it anyway would draw capsules through the model.
                bones = len(skeleton.region_set.bones) if skeleton else 0
                if skeleton is None or len(worn) < bones:
                    items.append(DrawItem(
                        kind=ENTITY, cell_id=cell.cell_id,
                        item_id=entry.entity_id, cell_placement=placement,
                        center=center, radius=radius,
                        distance=camera.distance_to(center)))
                drew_any = True

            for model_ref, (flat, worn_ids) in sorted(worn_rows.items()):
                radius, model_ref = _model_radius(library, model_ref,
                                                  ITEM_DRAW_RADIUS_M)
                result = place(_place_payload(camera, placement, radius, flat,
                                              lightmap))
                for slot, index_of in enumerate(result["visible"]):
                    base = index_of * 7
                    items.append(DrawItem(
                        kind=ENTITY, cell_id=cell.cell_id,
                        item_id=worn_ids[index_of], cell_placement=placement,
                        center=tuple(result["centers"][slot]), radius=radius,
                        distance=result["distances"][slot],
                        model_ref=model_ref,
                        transform=Transform(
                            position=(flat[base], flat[base + 1],
                                      flat[base + 2]),
                            rotation=(flat[base + 3], flat[base + 4],
                                      flat[base + 5], flat[base + 6]))))
                    if model_ref:
                        charge_placement(cell.cell_id)
                    drew_any = True
                if result["visible"] and _has_mesh(library, model_ref):
                    key = (cell.cell_id, model_ref)
                    packed[key] = packed.get(key, b"") + result["instances"]

        if drew_any:
            stats.cells_drawn += 1

    # Nearest first: a z-buffered opaque pass rejects the most pixels that way,
    # and a viewer that wants painter's order can reverse it.
    items.sort(key=lambda i: (i.distance, i.kind, i.item_id))
    stats.items_drawn = len(items)
    return DrawList(camera=camera, items=tuple(items), stats=stats,
                    instances=packed)
