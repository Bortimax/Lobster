"""World-space labels (Scope 8), which is a geometry question and only that.

> | **Lobster** | Object selection/raycasting, **world-space labels**,
> physically placing/removing an item's representation in the world. |
> | **Shrimp** | Inventory screens, equip menus, dialogue UI, all 2D chrome. |

The whole of what Lobster owns here is: **where would a label for this thing
go, how far away is it, and can you actually see the spot.** Four numbers and a
boolean.

What it does not own, and structurally cannot:

* **The text.** Lobster has never known a display name and §5 invariant 3
  forbids it naming a creature or an item at all.
* **Whether to draw it.** Distance falloff, fade curves, "only show hostiles",
  "hide while the menu is open" - every one of those is content deciding what
  matters (L4).
* **Priority.** There is no ordering by importance, for the same reason
  selection has none: nearest is the only order geometry supports.
* **Declutter.** Two labels overlapping on screen is a layout problem, and
  layout is 2D chrome. `screen_point` is handed over so Shrimp can compute
  overlap in two lines; computing it here would be the first step onto a slope
  that ends in Lobster owning a font stack.

**Occlusion reuses `Selector`.** A label is hidden when something stands between
the camera and its anchor, and "what is between these two points" is exactly the
question selection already answers. Sharing it means labels and picking can
never disagree about what you are looking at - if the crosshair says you are
pointing at the bandit, the bandit's label is not hidden behind a wall.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .geometry import Transform, Vec3, distance, normalize, sub
from .selection import ENTITY, ITEM, PROP, STRUCTURE, Selector

#: How far above a rig's head its anchor floats. Enough to clear the skull so
#: the occlusion ray does not hit the very thing it is labelling.
ENTITY_LABEL_LIFT_M = 0.25

#: The same for something lying on the ground.
ITEM_LABEL_LIFT_M = 0.30
PROP_LABEL_LIFT_M = 1.20

#: Nudge applied to the occlusion ray's reach, so a surface *at* the anchor
#: does not count as blocking it. Same reasoning as `_BOUND_EPSILON`: a test
#: exactly on a boundary is decided by rounding, and a gate must err one way on
#: purpose. Here it errs towards "visible", because a label that flickers off
#: when you look straight at something is worse than one that shows a frame too
#: long.
OCCLUSION_EPSILON_M = 1e-3


class LabelError(Exception):
    pass


@dataclass(frozen=True)
class LabelAnchor:
    """Where a label for one thing would go. Measurements, no opinions."""

    target_id: str
    kind: str
    cell_id: str
    #: world-space point the label hangs from
    world_point: Vec3
    #: (x, y) in pixels, origin top-left, or None when the anchor is at or
    #: behind the near plane. **Not the same as off screen**: an off-screen
    #: anchor projects fine and comes back with coordinates outside the
    #: viewport, which a caller may well want for an edge-of-screen marker.
    screen_point: Optional[Tuple[float, float]]
    #: metres from the camera
    distance: float
    #: something solid stands between the camera and the anchor
    occluded: bool

    @property
    def on_screen(self) -> bool:
        return self.screen_point is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"target_id": self.target_id, "kind": self.kind,
                "cell_id": self.cell_id,
                "world_point": list(self.world_point),
                "screen_point": (list(self.screen_point)
                                 if self.screen_point else None),
                "distance": self.distance, "occluded": self.occluded}


# ---------------------------------------------------------------------------
# Locating a target
# ---------------------------------------------------------------------------

def _entity_anchor(cell: Any, entity_id: str) -> Optional[Vec3]:
    skeleton = (getattr(cell, "skeletons", {}) or {}).get(entity_id)
    if skeleton is None:
        return None
    # The rig's own bound, not an assumed humanoid height - the same reasoning
    # that made the projectile gate rig-derived (D19). A label over a spider
    # belongs over the spider.
    capsule = skeleton.whole_body_capsule()
    top = capsule.a if capsule.a[1] >= capsule.b[1] else capsule.b
    return (top[0], top[1] + capsule.radius + ENTITY_LABEL_LIFT_M, top[2])


def _structure_anchor(cell: Any, structure_id: str) -> Optional[Vec3]:
    try:
        live = cell.structure(structure_id)
    except Exception:
        return None
    box = live.voxel_data.aabb_world()
    # Top centre: a label over a gatehouse belongs over the gatehouse, not
    # inside it, and anchoring at the centroid would put it behind its own
    # wall on every approach.
    return ((box.minimum[0] + box.maximum[0]) * 0.5,
            box.maximum[1] + ENTITY_LABEL_LIFT_M,
            (box.minimum[2] + box.maximum[2]) * 0.5)


def _prop_anchor(cell: Any, prop_id: str) -> Optional[Vec3]:
    for prop in getattr(getattr(cell, "bundle", None), "props", ()) or ():
        if prop.prop_id == prop_id:
            base = prop.transform.position
            return (base[0], base[1] + PROP_LABEL_LIFT_M, base[2])
    return None


def _item_anchor(view: Any, cell_id: str, item_id: str) -> Optional[Vec3]:
    if view is None:
        return None
    for record in view.items_in_location(cell_id):
        if record["id"] != item_id:
            continue
        raw = (record.get("world_transform") or {}).get("position")
        if not raw:
            return None
        return (float(raw[0]), float(raw[1]) + ITEM_LABEL_LIFT_M, float(raw[2]))
    return None


def locate(selector: Selector, target_id: str, *,
           view: Any = None) -> Optional[Tuple[str, str, Vec3]]:
    """`(kind, cell_id, world anchor)` for a target, or None if it is nowhere.

    Searches the resident cells in id order and takes the first match, so the
    answer is deterministic when two cells somehow claim the same id - which is
    a content defect, not something to arbitrate here.

    None is the honest answer for a target that is not resident: it has no
    position, so it has no anchor. That is not an error - an NPC walking out of
    the resident set is ordinary, and raising would make the caller handle an
    exception on a normal frame.
    """
    for cell_id in sorted(selector.cells):
        cell = selector.cells[cell_id]
        placement = selector.placement_of(cell_id)
        for kind, local in ((ENTITY, _entity_anchor(cell, target_id)),
                            (STRUCTURE, _structure_anchor(cell, target_id)),
                            (PROP, _prop_anchor(cell, target_id)),
                            (ITEM, _item_anchor(view, cell_id, target_id))):
            if local is not None:
                return kind, cell_id, placement.apply(local)
    return None


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

def label_anchors(camera: Any, selector: Selector, targets: Iterable[str], *,
                  view: Any = None, width: int = 1280, height: int = 720,
                  test_occlusion: bool = True) -> List[LabelAnchor]:
    """Where labels for `targets` would go, nearest first.

    A target that is not in any resident cell is **omitted**: it has no
    position, so there is no anchor to report. A caller that needs to know which
    ones dropped out can compare ids, and a label for something you cannot be
    standing near is not a case worth an exception.

    `test_occlusion=False` skips the visibility raycast, which is the whole
    cost of this function. Worth it for a caller that only wants screen
    positions - an off-screen edge marker does not care what is in front of the
    thing it points at.
    """
    out: List[LabelAnchor] = []
    for target_id in targets:
        found = locate(selector, target_id, view=view)
        if found is None:
            continue
        kind, cell_id, point = found
        projected = camera.project(point, width, height)
        out.append(LabelAnchor(
            target_id=target_id, kind=kind, cell_id=cell_id, world_point=point,
            screen_point=(projected[0], projected[1]) if projected else None,
            distance=camera.distance_to(point),
            occluded=(is_occluded(camera, selector, point, target_id,
                                  view=view) if test_occlusion else False)))
    out.sort(key=lambda a: (a.distance, a.target_id))
    return out


def is_occluded(camera: Any, selector: Selector, point: Vec3, target_id: str,
                *, view: Any = None) -> bool:
    """Does anything stand between the camera and this point?

    The label's own target does not count. An anchor floats just above the
    thing it labels, so a ray reaching it can clip the very shoulder it is
    hanging over - and a label hidden by its own subject would be a defect
    dressed as a feature.

    Terrain is included: a hill between you and a bandit hides the bandit's
    label exactly as a wall does.
    """
    reach = distance(camera.position, point) - OCCLUSION_EPSILON_M
    if reach <= 0.0:
        return False
    heading = sub(point, camera.position)
    if normalize(heading) == (0.0, 0.0, 0.0):
        return False
    for hit in selector.pick_all(camera.position, heading, reach, view=view):
        if hit.target_id != target_id:
            return True
    return False
