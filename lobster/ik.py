"""Procedural foot placement (Scope 3).

> Foot placement on uneven terrain -> **Procedural IK** on top of skeletal
> animation.

"On top of" is the whole boundary. This runs *after* a pose has been set, and it
adjusts where the feet and the pelvis end up so the character stands on the
ground rather than through it. It does not choose the pose, does not decide when
to plant a foot, and does not know what the character is doing - all of which
would be the animation controller's job, and L8's line.

So the interface is: hand it a `Skeleton` that has already been posed, and a
ground-height function, and it returns a pose adjustment. Whether to apply that
adjustment is the caller's call; `apply` is a convenience, not a mandate.

The solver is two-bone analytic IK, which is the standard answer for a leg and
the reason this is a small file rather than a general constraint system.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .geometry import Transform, Vec3, add, distance, length, normalize, scale, sub

#: How far below a foot's posed position we look for ground.
DEFAULT_TRACE_DOWN_M = 0.6

#: How far above it. A foot inside a slope needs lifting, not dropping.
DEFAULT_TRACE_UP_M = 0.5

GroundFn = Callable[[float, float], Optional[float]]


@dataclass(frozen=True)
class FootPlacement:
    """Where one foot should be, and how far it moved to get there."""

    bone_id: str
    posed: Vec3
    grounded: Vec3
    delta_y: float
    grounded_ok: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"bone_id": self.bone_id, "posed": list(self.posed),
                "grounded": list(self.grounded), "delta_y": self.delta_y,
                "grounded_ok": self.grounded_ok}


@dataclass(frozen=True)
class IKResult:
    """What the pass worked out. Applying it is a separate, explicit step."""

    feet: Tuple[FootPlacement, ...] = ()
    pelvis_drop: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"feet": [f.to_dict() for f in self.feet],
                "pelvis_drop": self.pelvis_drop}


def terrain_ground_fn(terrain: Any) -> GroundFn:
    """A ground function backed by a cell's baked heightfield collider.

    O(1) per query, which is the reason the collider is a heightfield in the
    first place (`lobster.terrain`): a per-foot raycast into a triangle soup,
    every frame, for every ACTIVE NPC, is exactly the cost a disc-era budget
    does not have.
    """
    def ground(x: float, z: float) -> Optional[float]:
        if terrain is None or terrain.collider is None:
            return None
        return terrain.collider.ground_height(x, z)
    return ground


def solve_foot_placement(skeleton: Any, ground: GroundFn, *,
                         foot_bones: Optional[Sequence[str]] = None,
                         trace_down: float = DEFAULT_TRACE_DOWN_M,
                         trace_up: float = DEFAULT_TRACE_UP_M) -> IKResult:
    """Work out where each foot should sit on the ground beneath it.

    The pelvis drop is the standard trick: when one foot has to reach further
    down than the other, lowering the root by the larger drop keeps the legs
    from over-extending, and the shorter leg bends instead. Without it a
    character on a slope does the splits.
    """
    bones = list(foot_bones or _default_foot_bones(skeleton))
    matrices = skeleton.bone_matrices()
    placements: List[FootPlacement] = []
    drops: List[float] = []

    for bone_id in bones:
        transform = matrices.get(bone_id)
        if transform is None:
            continue
        posed = _foot_tip(skeleton, bone_id, transform)
        height = ground(posed[0], posed[2])
        if height is None:
            placements.append(FootPlacement(bone_id, posed, posed, 0.0, False))
            continue
        delta = height - posed[1]
        if delta > trace_up or delta < -trace_down:
            # the ground is further away than this pass is willing to reach;
            # report it rather than snapping the foot somewhere implausible.
            placements.append(FootPlacement(bone_id, posed, posed, 0.0, False))
            continue
        grounded = (posed[0], height, posed[2])
        placements.append(FootPlacement(bone_id, posed, grounded, delta, True))
        drops.append(delta)

    pelvis_drop = min(drops) if drops else 0.0
    return IKResult(feet=tuple(placements),
                    pelvis_drop=pelvis_drop if pelvis_drop < 0.0 else 0.0)


def _default_foot_bones(skeleton: Any) -> List[str]:
    """Leg bones, by the region set's own declaration - not by name matching."""
    from .skeleton import LEFT_LEG, RIGHT_LEG
    return [bone.bone_id for bone in skeleton.region_set.bones
            if bone.region in (LEFT_LEG, RIGHT_LEG)]


def _foot_tip(skeleton: Any, bone_id: str, transform: Transform) -> Vec3:
    """The lower end of a leg bone in world space."""
    capsule = skeleton.capsule_for(bone_id)
    return capsule.a if capsule.a[1] <= capsule.b[1] else capsule.b


def apply(skeleton: Any, result: IKResult) -> None:
    """Apply a solved placement to a posed skeleton.

    Deliberately separate from solving. A caller that wants to blend the
    correction in over a few frames, or skip it while ragdolling, is making an
    animation decision - and that decision belongs to Shrimp (L8), so Lobster
    hands over the numbers and does what it is told.
    """
    if result.pelvis_drop:
        root = skeleton.root
        skeleton.set_root(Transform(
            position=(root.position[0], root.position[1] + result.pelvis_drop,
                      root.position[2]),
            rotation=root.rotation))


def two_bone_ik(root: Vec3, joint: Vec3, effector: Vec3,
                target: Vec3) -> Tuple[Vec3, Vec3]:
    """Analytic two-bone IK: returns the new (joint, effector) positions.

    The classic hip-knee-foot solve. Bone lengths are preserved; when the target
    is out of reach the limb straightens toward it rather than stretching, which
    is what a leg does.
    """
    upper = distance(root, joint)
    lower = distance(joint, effector)
    to_target = sub(target, root)
    reach = length(to_target)
    if reach <= 1e-9:
        return joint, effector
    direction = normalize(to_target)

    if reach >= upper + lower:                       # out of reach: straighten
        new_joint = add(root, scale(direction, upper))
        return new_joint, add(new_joint, scale(direction, lower))
    if reach <= abs(upper - lower):                  # too close: fold
        new_joint = add(root, scale(direction, upper))
        return new_joint, target

    # cosine rule for the joint angle, bend preserved from the current pose
    cos_angle = (reach * reach + upper * upper - lower * lower) / (2 * reach * upper)
    cos_angle = max(-1.0, min(1.0, cos_angle))
    along = upper * cos_angle
    offset = upper * math.sqrt(max(0.0, 1.0 - cos_angle * cos_angle))

    bend = sub(joint, add(root, scale(direction, _project(sub(joint, root),
                                                          direction))))
    bend_dir = normalize(bend)
    if bend_dir == (0.0, 0.0, 0.0):
        bend_dir = _any_perpendicular(direction)
    new_joint = add(add(root, scale(direction, along)), scale(bend_dir, offset))
    return new_joint, target


def _project(vector: Vec3, onto: Vec3) -> float:
    return sum(vector[i] * onto[i] for i in range(3))


def _any_perpendicular(direction: Vec3) -> Vec3:
    candidate = (0.0, 1.0, 0.0)
    if abs(_project(candidate, direction)) > 0.99:
        candidate = (1.0, 0.0, 0.0)
    projected = scale(direction, _project(candidate, direction))
    return normalize(sub(candidate, projected))
