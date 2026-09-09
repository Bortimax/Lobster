"""Object selection and raycasting (Scope 8).

> | **Lobster** | Object selection/raycasting, world-space labels, physically
> placing/removing an item's representation in the world. |

This is the first of those three: *what is under this ray*. It is what lets
Lobster raise `on_interact` itself rather than declaring an Event and waiting for
a caller to notice something (DECISIONS.md D24, D25).

**It is not hit-testing, and the two must not be merged.** A hit-test asks "does
this attack connect, how hard, on which limb", is filtered by tier, and reports
force. Selection asks "what am I pointing at", considers everything in the world
rather than only rigged bodies, and reports no force at all. They share the ray
maths and nothing else.

**It decides nothing about interactability.** Whether a barrel opens, whether an
NPC will talk, whether a door is locked - none of that is here and none of it can
be. Lobster reports *"the nearest thing along this ray is `X`, at this point, at
this distance"*; what that means is Shrimp's and Octopus's (L4). There is no
`is_interactable`, no filter by "usable", and no priority ordering by importance:
the nearest thing wins, because that is the only ordering geometry supports.

**Cross-cell from the start.** You can point at whatever you can see, and since
exterior cells are placed (D21) that includes the next valley. The ray is
transformed into each cell's space exactly as `WorldHitTester` does it (D23) -
the ray moves, the world does not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Tuple)

from .constants import VOXEL_SIZE_M
from .events import EventBus
from .geometry import (AABB, Transform, Vec3, add, distance, normalize,
                       ray_capsule_hit, scale, segment_aabb_overlap, sub)
from .octopus_bridge import HIT_TEST_ONLY
from .tiers import DORMANT

#: What a selection stands for.
ENTITY = "entity"
STRUCTURE = "structure"
PROP = "prop"
ITEM = "item"
TERRAIN = "terrain"

SELECTION_KINDS: Tuple[str, ...] = (ENTITY, STRUCTURE, PROP, ITEM, TERRAIN)

#: How close a ray must pass to a prop's origin to count as pointing at it.
#: Props declare no extent anywhere in the bundle, so this is the one number
#: selection has to invent - and it is declared here rather than buried.
PROP_PICK_RADIUS_M = 0.5

#: The same, for a placed item. Separate from `PROP_PICK_RADIUS_M` and smaller,
#: because the things differ: a prop is a barrel or a crate, an item is a sword
#: on a table. Neither declares an extent - `Item.model_ref` resolves to nothing
#: until there is an asset pipeline (D34) - so both are invented numbers, and
#: keeping them separate means tuning one never silently moves the other.
ITEM_PICK_RADIUS_M = 0.35

#: How many bucket lookups one item's capsule test is worth, for choosing
#: between the grid and a linear scan. Measured: a bucket lookup is ~0.2 us and
#: an item's capsule test ~4.2 us, so a walk that touches more than ~21 buckets
#: per item present is not worth taking. Rounded down, because the grid also
#: pays a build (D38).
SCAN_BUCKETS_PER_ITEM = 20

#: Step used when marching a ray against the terrain heightfield.
TERRAIN_MARCH_STEP_M = 0.25


class SelectionError(Exception):
    pass


@dataclass(frozen=True)
class Selection:
    """What a ray found. Measurements only - no opinion about what it means."""

    kind: str
    target_id: str
    cell_id: str
    #: world-space point where the ray met the surface
    point: Vec3
    #: surface normal at that point, world space; zero where none was measured
    normal: Vec3
    distance: float
    #: which micro-chunk, for a structure. This is what damage takes, so a pick
    #: can be turned into a hit without a second query.
    chunk_index: Optional[int] = None
    #: which of the rig's regions, for an entity
    region: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "target_id": self.target_id,
                "cell_id": self.cell_id, "point": list(self.point),
                "normal": list(self.normal), "distance": self.distance,
                "chunk_index": self.chunk_index, "region": self.region}


# ---------------------------------------------------------------------------
# Voxel traversal
# ---------------------------------------------------------------------------

def raymarch_structure(live: Any, origin: Vec3, direction: Vec3,
                       max_distance: float, *,
                       voxel_size: float = VOXEL_SIZE_M
                       ) -> Optional[Tuple[Vec3, Vec3, int, float]]:
    """March a ray through one structure's voxels, in **structure-local** space.

    Amanatides-Woo: the standard grid traversal, which visits every voxel the ray
    passes through in order and no others. Solidity is read through
    `LiveStructure.is_solid`, so break-state is already applied - a ray goes
    straight through a destroyed chunk, which is what makes a pick agree with
    what is on screen.

    Returns `(point, normal, chunk_index, distance)` or None.
    """
    data = live.voxel_data
    grid = data.grid_size
    extent = grid * voxel_size
    box = AABB((0.0, 0.0, 0.0), (extent, extent, extent))

    heading = normalize(direction)
    if heading == (0.0, 0.0, 0.0):
        raise SelectionError("a ray needs a direction")
    end = add(origin, scale(heading, max_distance))
    if not segment_aabb_overlap(origin, end, box):
        return None

    # advance to the grid if the ray starts outside it
    start, entry_t, entry_axis = origin, 0.0, 0
    if not box.contains(origin):
        entry = _entry_distance(origin, heading, box)
        if entry is None or entry[0] > max_distance:
            return None
        entry_t, entry_axis = entry
        start = add(origin, scale(heading, entry_t + 1e-6))

    voxel = [int(math.floor(start[i] / voxel_size)) for i in range(3)]
    step = [0, 0, 0]
    t_max = [math.inf, math.inf, math.inf]
    t_delta = [math.inf, math.inf, math.inf]
    for i in range(3):
        if heading[i] > 1e-12:
            step[i] = 1
            t_max[i] = ((voxel[i] + 1) * voxel_size - start[i]) / heading[i]
            t_delta[i] = voxel_size / heading[i]
        elif heading[i] < -1e-12:
            step[i] = -1
            t_max[i] = (voxel[i] * voxel_size - start[i]) / heading[i]
            t_delta[i] = -voxel_size / heading[i]

    travelled = 0.0
    axis = entry_axis
    budget = int(max_distance / voxel_size) * 3 + 8
    for _ in range(budget):
        if all(0 <= voxel[i] < grid for i in range(3)):
            if live.is_solid(voxel[0], voxel[1], voxel[2]):
                total = entry_t + travelled
                point = add(origin, scale(heading, total))
                normal = [0.0, 0.0, 0.0]
                normal[axis] = float(-step[axis])
                return (point, tuple(normal),
                        data.chunk_of_voxel(voxel[0], voxel[1], voxel[2]),
                        total)
        axis = min(range(3), key=lambda i: t_max[i])
        if t_max[axis] is math.inf:
            return None
        travelled = t_max[axis]
        if entry_t + travelled > max_distance:
            return None
        voxel[axis] += step[axis]
        t_max[axis] += t_delta[axis]
        if not (-1 <= voxel[axis] <= grid):
            return None
    return None


def _entry_distance(origin: Vec3, heading: Vec3,
                    box: AABB) -> Optional[Tuple[float, int]]:
    """Distance along a ray to where it enters a box, and through which axis.

    The axis matters: if the very first voxel the ray reaches is solid, the
    surface it struck is the box's entry face, and without it the traversal has
    no last-stepped axis to derive a normal from - so a wall hit head-on
    reported a normal of (0, 0, 0).
    """
    t_min, t_max, axis = 0.0, math.inf, 0
    for i in range(3):
        if abs(heading[i]) < 1e-12:
            if origin[i] < box.minimum[i] or origin[i] > box.maximum[i]:
                return None
            continue
        near = (box.minimum[i] - origin[i]) / heading[i]
        far = (box.maximum[i] - origin[i]) / heading[i]
        if near > far:
            near, far = far, near
        if near > t_min:
            t_min, axis = near, i
        t_max = min(t_max, far)
        if t_min > t_max:
            return None
    return t_min, axis


def raymarch_terrain(terrain: Any, origin: Vec3, direction: Vec3,
                     max_distance: float, *,
                     step_m: float = TERRAIN_MARCH_STEP_M
                     ) -> Optional[Tuple[Vec3, float]]:
    """Where a ray first goes below the terrain heightfield, in cell space.

    A march rather than an analytic solve: the collider is a heightfield, so
    "below the ground" is a comparison at any point, and the crossing is found
    by stepping until the sign changes and then bisecting. Cheap, and it makes
    no assumption about the surface being planar.
    """
    collider = getattr(terrain, "collider", None)
    if collider is None:
        return None
    heading = normalize(direction)
    if heading == (0.0, 0.0, 0.0):
        raise SelectionError("a ray needs a direction")

    def below(t: float) -> bool:
        p = add(origin, scale(heading, t))
        if not collider.covers(p[0], p[2]):
            return False        # outside this cell's terrain; not ours to claim
        return p[1] <= collider.ground_height(p[0], p[2])

    if below(0.0):
        return None                          # started underground
    travelled = 0.0
    while travelled < max_distance:
        nxt = min(travelled + step_m, max_distance)
        if below(nxt):
            low, high = travelled, nxt
            for _ in range(24):              # bisect to sub-millimetre
                mid = (low + high) * 0.5
                if below(mid):
                    high = mid
                else:
                    low = mid
            point = add(origin, scale(heading, high))
            return point, high
        travelled = nxt
    return None


# ---------------------------------------------------------------------------
# The selector
# ---------------------------------------------------------------------------

class Selector:
    """Answers "what is under this ray" across every resident cell.

    Constructed the same way `WorldHitTester` is, and for the same reason: a
    player can point at anything they can see, and with exterior cells placed
    (D21) that reaches past the cell boundary.
    """

    def __init__(self, cells: Iterable[Any],
                 placements: Optional[Mapping[str, Transform]] = None) -> None:
        self.cells = {cell.cell_id: cell for cell in cells}
        self.placements = dict(placements or {})
        self._identity = Transform()

    @classmethod
    def from_manager(cls, manager: Any, view: Any) -> "Selector":
        return cls([manager.resident[c] for c in sorted(manager.resident)],
                   manager.placements(view))

    def placement_of(self, cell_id: str) -> Transform:
        return self.placements.get(cell_id, self._identity)

    # -- picking -------------------------------------------------------------
    def pick(self, origin: Vec3, direction: Vec3, max_distance: float, *,
             kinds: Optional[Sequence[str]] = None,
             view: Any = None) -> Optional[Selection]:
        """The nearest thing along the ray, or None.

        Nearest wins. That is the only ordering geometry supports; any other -
        "prefer NPCs", "prefer usable things" - would be Lobster deciding what
        matters, which is content's call (L4).

        `view` is optional and used only to read `limb_state`, so a pick never
        offers a severed limb as a target. Without it every rig region is
        pickable, which is the right default for a tool or a test.
        """
        found = self.pick_all(origin, direction, max_distance, kinds=kinds,
                              view=view)
        return found[0] if found else None

    def pick_all(self, origin: Vec3, direction: Vec3, max_distance: float, *,
                 kinds: Optional[Sequence[str]] = None,
                 view: Any = None) -> List[Selection]:
        """Everything along the ray, nearest first."""
        wanted = set(kinds) if kinds else set(SELECTION_KINDS)
        for kind in sorted(wanted):
            if kind not in SELECTION_KINDS:
                raise SelectionError(
                    "{0!r} is not a selection kind; the set is {1}".format(
                        kind, list(SELECTION_KINDS)))
        heading = normalize(direction)
        if heading == (0.0, 0.0, 0.0):
            raise SelectionError("a ray needs a direction")

        out: List[Selection] = []
        for cell_id in sorted(self.cells):
            cell = self.cells[cell_id]
            placement = self.placement_of(cell_id)
            local_origin = placement.inverse_apply(origin)
            local_heading = placement.inverse_rotate(heading)

            if STRUCTURE in wanted:
                out.extend(self._structures(cell, placement, local_origin,
                                            local_heading, max_distance))
            if ENTITY in wanted:
                out.extend(self._entities(cell, placement, local_origin,
                                          local_heading, max_distance, view))
            if PROP in wanted:
                out.extend(self._props(cell, placement, local_origin,
                                       local_heading, max_distance))
            if ITEM in wanted:
                out.extend(self._items(cell, placement, local_origin,
                                       local_heading, max_distance, view))
            if TERRAIN in wanted:
                out.extend(self._terrain(cell, placement, local_origin,
                                         local_heading, max_distance))
        out.sort(key=lambda s: (s.distance, s.kind, s.target_id))
        return out

    # -- per kind ------------------------------------------------------------
    def _structures(self, cell: Any, placement: Transform, origin: Vec3,
                    heading: Vec3, max_distance: float) -> List[Selection]:
        out: List[Selection] = []
        for structure_id, live in sorted(getattr(cell, "structures", {}).items()):
            local = live.voxel_data.origin
            hit = raymarch_structure(
                live, local.inverse_apply(origin),
                local.inverse_rotate(heading), max_distance)
            if hit is None:
                continue
            point, normal, chunk_index, dist = hit
            out.append(Selection(
                kind=STRUCTURE, target_id=structure_id, cell_id=cell.cell_id,
                point=placement.apply(local.apply(point)),
                normal=placement.rotate(local.rotate(normal)),
                distance=dist, chunk_index=chunk_index))
        return out

    def _entities(self, cell: Any, placement: Transform, origin: Vec3,
                  heading: Vec3, max_distance: float,
                  view: Any) -> List[Selection]:
        index = getattr(cell, "index", None)
        skeletons = getattr(cell, "skeletons", {})
        if index is None:
            return []
        out: List[Selection] = []
        for entry in index.entries():
            skeleton = skeletons.get(entry.entity_id)
            if skeleton is None:
                continue          # nothing to point at; DORMANT carries no rig
            limb_state = None
            if view is not None:
                limb_state = view.limb_state(entry.entity_id,
                                             purpose=HIT_TEST_ONLY)
            best = None
            for region, capsule in skeleton.hitboxes(limb_state=limb_state):
                if not ray_capsule_hit(origin, heading, capsule, max_distance):
                    continue
                centre = tuple((capsule.a[i] + capsule.b[i]) * 0.5
                               for i in range(3))
                along = distance(origin, centre)
                if best is None or along < best[0]:
                    best = (along, region, centre)
            if best is None:
                continue
            along, region, centre = best
            out.append(Selection(
                kind=ENTITY, target_id=entry.entity_id, cell_id=cell.cell_id,
                point=placement.apply(centre), normal=(0.0, 0.0, 0.0),
                distance=along, region=region))
        return out

    def _props(self, cell: Any, placement: Transform, origin: Vec3,
               heading: Vec3, max_distance: float) -> List[Selection]:
        from .geometry import Capsule
        bundle = getattr(cell, "bundle", None)
        out: List[Selection] = []
        for prop in getattr(bundle, "props", ()) or ():
            base = prop.transform.position
            volume = Capsule(base, (base[0], base[1] + 1.0, base[2]),
                             PROP_PICK_RADIUS_M)
            if not ray_capsule_hit(origin, heading, volume, max_distance):
                continue
            out.append(Selection(
                kind=PROP, target_id=prop.prop_id, cell_id=cell.cell_id,
                point=placement.apply(base), normal=(0.0, 0.0, 0.0),
                distance=distance(origin, base)))
        return out

    def _items(self, cell: Any, placement: Transform, origin: Vec3,
               heading: Vec3, max_distance: float,
               view: Any) -> List[Selection]:
        """Items physically placed in this cell (Scope 8, D33/D35).

        **Distinct from a prop, and the distinction is the point.** A prop is
        decoration baked into the bundle with no gameplay identity - the
        `PropPlacement` docstring has always said so: *"Anything the player can
        pick up, open or be told about is an Octopus `Item` record [...] it does
        not live here."* Collapsing them would erase a line the code already
        drew, and `target_id` would stop being a record id you can resolve.

        **Needs a `view`**, because items live in records rather than in the
        bundle. Without one there is nothing to read and no items are pickable -
        the same shape as `_entities`, where a missing view means every rig
        region is offered. A tool or a test that passes no view is asking a
        question about baked geometry, and items are not baked.
        """
        if view is None:
            return []
        from .geometry import Capsule
        grid = view.item_grid(cell.cell_id) if hasattr(view, "item_grid") else None
        if grid is not None and len(grid) == 0:
            # The common case for most resident cells, and worth its own exit:
            # an empty grid still costs a full bucket walk otherwise, and that
            # walk is what dominates a long pick (D38).
            return []

        end = tuple(origin[i] + heading[i] * max_distance for i in range(3))
        if grid is not None and grid.walk_cost(origin, end, ITEM_PICK_RADIUS_M)                 <= len(grid) * SCAN_BUCKETS_PER_ITEM:
            candidates = grid.near_segment(origin, end, ITEM_PICK_RADIUS_M)
        else:
            # A long ray through a thin scatter: the walk costs more than
            # testing everything. Measured, not guessed - a 120 m pick over 25
            # items is 2x slower through the grid (D38).
            candidates = [(r["id"],
                           tuple(float(c) for c in
                                 (r.get("world_transform") or {})["position"]))
                          for r in view.items_in_location(cell.cell_id)
                          if (r.get("world_transform") or {}).get("position")]

        out: List[Selection] = []
        for item_id, base in candidates:
            volume = Capsule(base, (base[0], base[1] + 0.5, base[2]),
                             ITEM_PICK_RADIUS_M)
            if not ray_capsule_hit(origin, heading, volume, max_distance):
                continue
            out.append(Selection(
                kind=ITEM, target_id=item_id, cell_id=cell.cell_id,
                point=placement.apply(base), normal=(0.0, 0.0, 0.0),
                distance=distance(origin, base)))
        return out

    def _terrain(self, cell: Any, placement: Transform, origin: Vec3,
                 heading: Vec3, max_distance: float) -> List[Selection]:
        terrain = getattr(cell, "terrain", None)
        if terrain is None:
            return []
        hit = raymarch_terrain(terrain, origin, heading, max_distance)
        if hit is None:
            return []
        point, dist = hit
        return [Selection(kind=TERRAIN, target_id=cell.cell_id,
                          cell_id=cell.cell_id, point=placement.apply(point),
                          normal=(0.0, 1.0, 0.0), distance=dist)]

    # -- the Event -----------------------------------------------------------
    def interact(self, bus: EventBus, origin: Vec3, direction: Vec3,
                 max_distance: float, *, view: Any = None,
                 kinds: Optional[Sequence[str]] = None) -> Optional[Selection]:
        """The player used whatever is under this ray. Picks, then reports.

        This is the call that lets Lobster raise `on_interact` itself. The
        *decision* that an interaction happened stays outside - only the consumer
        knows a button was pressed, exactly as Octopus's D31 note says of walking
        through a door. Lobster resolves what was pointed at and reports it.

        Terrain is excluded by default: "the player interacted with the ground"
        is not something `on_interact(target_id)` can say usefully, since the
        target id would be the cell. Pass `kinds` explicitly to include it.
        """
        selectable = kinds if kinds is not None else (ENTITY, STRUCTURE,
                                                      PROP, ITEM)
        found = self.pick(origin, direction, max_distance, kinds=selectable,
                          view=view)
        if found is not None:
            bus.interact(found.target_id)
        return found
