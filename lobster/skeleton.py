"""Skeleton pose data and hitboxes (Scope 5, L8).

The boundary this module exists to hold, quoted in full because it is the one
most likely to erode:

> **Lobster** | Swing volumes, hitbox regions (ACTIVE tier), "region X hit for Y
> force," structure voxel damage + remesh, **skeleton pose data** (bone
> matrices) exposed via a `set_pose` interface, reading live limb state **only
> to decide which hitboxes exist for the next test**.
>
> **Shrimp - animation controller** | The state machine: which clip plays, blend
> weights, when to transition to ragdoll, what "disabled arm" looks like.

So `Skeleton` has exactly one mutator - `set_pose` - and it takes a finished
pose. There is no clip, no blend weight, no transition, no "on damage do X".
Nothing in this module reads `limb_state`, and nothing in it can: the hit-test
passes the limb map in as an argument (`hitboxes(limb_state=...)`), so the only
code that can produce one is code holding a `FrameView` and the
`HIT_TEST_ONLY` sentinel. L8, mechanically.

If a future change wants a `Skeleton.play(clip)` or a `Skeleton.on_hit(...)`,
that is Shrimp, and the answer is no.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .geometry import Capsule, Transform, Vec3
from .octopus_bridge import limb_has_hitbox

# ---------------------------------------------------------------------------
# Hit regions - Lobster-owned vocabulary (Scope 5)
# ---------------------------------------------------------------------------

HEAD = "head"
TORSO = "torso"
LEFT_ARM = "left_arm"
RIGHT_ARM = "right_arm"
LEFT_LEG = "left_leg"
RIGHT_LEG = "right_leg"

#: "Hit-region vocabulary stays a Lobster-owned enum (6 humanoid regions)."
#: Frozen at six - no `whole_body` sentinel was ever added, because every tier
#: with a hitbox now resolves to one of these six (DECISIONS.md D16).
HUMANOID_REGIONS: Tuple[str, ...] = (HEAD, TORSO, LEFT_ARM, RIGHT_ARM,
                                     LEFT_LEG, RIGHT_LEG)


#: How the seam kernel hands a capsule back: `a`, `b`, radius, as float64.
#: Mirrors `conformance.CAPSULE_FLOATS`/`CAPSULE_PACK`; a test pins them
#: equal, because a wire format spelled in two files is two wire formats.
_CAPSULE_FORMAT = "7d"
_CAPSULE_WIDTH = struct.calcsize(_CAPSULE_FORMAT)


class SkeletonError(Exception):
    pass


@dataclass(frozen=True)
class Bone:
    """One bone: a rest-space capsule, a region, and a limb id.

    `limb_id` is the key into `Character.limb_state`. Several bones may share
    one limb id (upper and lower arm are one arm to sever), and a bone with no
    limb id - the pelvis, the spine - is never severable.
    """

    bone_id: str
    region: str
    a: Vec3
    b: Vec3
    radius: float
    limb_id: Optional[str] = None
    parent: Optional[str] = None

    def rest_capsule(self) -> Capsule:
        return Capsule(self.a, self.b, self.radius)

    def to_dict(self) -> Dict[str, Any]:
        return {"bone_id": self.bone_id, "region": self.region,
                "a": list(self.a), "b": list(self.b), "radius": self.radius,
                "limb_id": self.limb_id, "parent": self.parent}


@dataclass(frozen=True)
class RestExtent:
    """How much space a rig actually occupies, in rest space.

    Every gating volume in Lobster is derived from this rather than assumed.
    Before it existed, `whole_body_capsule` took its radius from
    `max(bone.radius)` - which bounds a rig only if every bone lies within the
    widest bone's radius of the vertical axis. That is a humanoid assumption,
    and it was stated nowhere: a wide creature was silently unshootable at
    range, and even the humanoid overhung its own gate by 17 cm of arm. See
    DECISIONS.md D19.
    """

    #: lowest and highest points of the rig, bone radii included
    min_y: float
    max_y: float
    #: furthest any bone reaches from the vertical axis, its radius included
    horizontal_reach: float
    #: radius of a sphere at the entity's root (its feet) containing the whole
    #: rig - what a broad phase measuring to an entity's position needs
    bound_radius: float

    def height(self) -> float:
        return self.max_y - self.min_y

    def to_dict(self) -> Dict[str, Any]:
        return {"min_y": self.min_y, "max_y": self.max_y,
                "horizontal_reach": self.horizontal_reach,
                "bound_radius": self.bound_radius}


#: A micron of slack on every derived bound.
#:
#: An exactly-touching bound is decided by floating-point rounding: the
#: humanoid's arm reaches 0.288 + 0.081, and whether that lands inside a
#: 0.369-radius capsule depends on which order the two sums were evaluated in.
#: For a *gate* the safe direction is outward - over-inclusion costs one
#: refinement pass that finds nothing, under-inclusion silently loses a hit.
_BOUND_EPSILON = 1e-6


def _rest_extent(bones: Sequence[Bone]) -> RestExtent:
    """Measure a rig. Pure, and computed once per region set."""
    if not bones:
        return RestExtent(0.0, 0.0, 0.0, 0.0)
    min_y = min(min(b.a[1], b.b[1]) - b.radius for b in bones)
    max_y = max(max(b.a[1], b.b[1]) + b.radius for b in bones)
    horizontal = max(math.hypot(p[0], p[2]) + b.radius
                     for b in bones for p in (b.a, b.b))
    bound = max(math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2) + b.radius
                for b in bones for p in (b.a, b.b))
    return RestExtent(min_y=min_y - _BOUND_EPSILON,
                      max_y=max_y + _BOUND_EPSILON,
                      horizontal_reach=horizontal + _BOUND_EPSILON,
                      bound_radius=bound + _BOUND_EPSILON)


@dataclass(frozen=True)
class RegionSet:
    """A creature type's declared hit regions and its bones.

    "non-humanoid creatures get their own declared region sets at content-time,
    the mechanism stays Lobster's" - so this is data a content package supplies,
    and the only thing hard-coded here is the humanoid default.

    `extent` is measured once here, at construction, because every gating volume
    downstream needs it and a rig's rest pose never changes.
    """

    name: str
    regions: Tuple[str, ...]
    bones: Tuple[Bone, ...]
    extent: Optional[RestExtent] = dc_field(default=None, init=False,
                                            compare=False, repr=False)
    #: the bones as flat kernel input - `a`, `b`, radius per bone, in
    #: `bones` order. Built here for the reason `extent` is: a rest pose never
    #: changes, and rebuilding it per call would be per-bone Python work in
    #: front of a kernel that exists to remove per-bone Python work (D54).
    rest_rows: Tuple[float, ...] = dc_field(default=(), init=False,
                                            compare=False, repr=False)

    def __post_init__(self) -> None:
        declared = set(self.regions)
        for bone in self.bones:
            if bone.region not in declared:
                raise SkeletonError(
                    "region set {0!r}: bone {1!r} declares region {2!r}, which "
                    "is not in the set's declared regions {3}".format(
                        self.name, bone.bone_id, bone.region,
                        list(self.regions)))
        object.__setattr__(self, "extent", _rest_extent(self.bones))
        rows: List[float] = []
        for bone in self.bones:
            rows.extend((float(bone.a[0]), float(bone.a[1]), float(bone.a[2]),
                         float(bone.b[0]), float(bone.b[1]), float(bone.b[2]),
                         float(bone.radius)))
        object.__setattr__(self, "rest_rows", tuple(rows))

    def bone(self, bone_id: str) -> Bone:
        for b in self.bones:
            if b.bone_id == bone_id:
                return b
        raise SkeletonError("region set {0!r}: no bone {1!r}".format(
            self.name, bone_id))

    def limb_ids(self) -> List[str]:
        seen: Dict[str, None] = {}
        for b in self.bones:
            if b.limb_id:
                seen.setdefault(b.limb_id)
        return list(seen)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "regions": list(self.regions),
                "bones": [b.to_dict() for b in self.bones]}


def humanoid_region_set(*, height: float = 1.8) -> RegionSet:
    """The default six-region humanoid rig, scaled to `height`.

    Proportions only; nothing here is a gameplay statement. A creature that
    wants different ones ships its own `RegionSet`.
    """
    h = height
    return RegionSet(
        name="humanoid",
        regions=HUMANOID_REGIONS,
        bones=(
            Bone("head", HEAD, (0.0, h * 0.88, 0.0), (0.0, h, 0.0), h * 0.075,
                 limb_id=None, parent="torso"),
            Bone("torso", TORSO, (0.0, h * 0.5, 0.0), (0.0, h * 0.88, 0.0),
                 h * 0.11, limb_id=None),
            Bone("arm_l", LEFT_ARM, (-h * 0.11, h * 0.85, 0.0),
                 (-h * 0.16, h * 0.48, 0.0), h * 0.045,
                 limb_id="left_arm", parent="torso"),
            Bone("arm_r", RIGHT_ARM, (h * 0.11, h * 0.85, 0.0),
                 (h * 0.16, h * 0.48, 0.0), h * 0.045,
                 limb_id="right_arm", parent="torso"),
            Bone("leg_l", LEFT_LEG, (-h * 0.06, h * 0.5, 0.0),
                 (-h * 0.07, 0.0, 0.0), h * 0.055,
                 limb_id="left_leg", parent="torso"),
            Bone("leg_r", RIGHT_LEG, (h * 0.06, h * 0.5, 0.0),
                 (h * 0.07, 0.0, 0.0), h * 0.055,
                 limb_id="right_leg", parent="torso"),
        ))


# ---------------------------------------------------------------------------
# Pose
# ---------------------------------------------------------------------------

class Skeleton:
    """Pose data for one entity. One mutator: `set_pose`.

    The pose is a plain `{bone_id: Transform}` map in entity-local space, and
    `root` places the entity in the cell. Bone matrices come out of
    `bone_matrices()` in world space, which is everything a renderer, a
    procedural-IK pass or a hit-test needs and nothing an animation controller
    would need to be given rather than to compute.
    """

    def __init__(self, entity_id: str, region_set: Optional[RegionSet] = None,
                 *, root: Optional[Transform] = None) -> None:
        self.entity_id = entity_id
        self.region_set = region_set or humanoid_region_set()
        self.root = root or Transform()
        self._pose: Dict[str, Transform] = {
            b.bone_id: Transform() for b in self.region_set.bones}
        #: bumped on every pose write, so a consumer can tell a stale read.
        self.pose_version = 0
        #: the pose as flat kernel input, and the version it was built from.
        #: `pose_version` existed for exactly this - "so a consumer can tell a
        #: stale read" - and this is the first consumer to use it (D54).
        self._pose_rows: Tuple[float, ...] = ()
        self._pose_rows_version = -1

    # -- the interface Shrimp binds against ----------------------------------
    def set_pose(self, pose: Mapping[str, Transform], *,
                 root: Optional[Transform] = None) -> None:
        """Adopt a pose. Lobster stores it; it never chooses it.

        `pose` maps bone ids to entity-local transforms. Bones left out keep
        their current transform, so a caller may pose a subset. An unknown bone
        id raises rather than being ignored - a controller posing a bone this
        rig does not have is a content bug, and a silently dropped bone is the
        kind of thing that surfaces as "the left arm does not move" three weeks
        later.
        """
        for bone_id, transform in pose.items():
            if bone_id not in self._pose:
                raise SkeletonError(
                    "{0}: no bone {1!r} in region set {2!r}".format(
                        self.entity_id, bone_id, self.region_set.name))
            if not isinstance(transform, Transform):
                raise SkeletonError(
                    "{0}: pose for bone {1!r} must be a Transform".format(
                        self.entity_id, bone_id))
            self._pose[bone_id] = transform
        if root is not None:
            self.root = root
        self.pose_version += 1

    def set_root(self, root: Transform) -> None:
        self.root = root
        self.pose_version += 1

    def pose(self) -> Dict[str, Transform]:
        return dict(self._pose)

    def bone_matrices(self) -> Dict[str, Transform]:
        """World-space transform per bone. What a renderer and IK consume."""
        return {bone_id: Transform(
            position=self.root.apply(local.position),
            rotation=local.rotation)
            for bone_id, local in self._pose.items()}

    # -- hitboxes ------------------------------------------------------------
    def capsule_for(self, bone_id: str) -> Capsule:
        """One bone's world-space capsule.

        Composes root and bone once rather than applying two transforms to each
        endpoint - three rotations instead of four, and the same definition the
        seam kernel and its reference use, so there is one transform here and
        not two that can drift.
        """
        bone = self.region_set.bone(bone_id)
        placed = self.root.compose(self._pose[bone_id])
        return Capsule(placed.apply(bone.a), placed.apply(bone.b), bone.radius)

    def pose_rows(self) -> Tuple[float, ...]:
        """The pose as flat kernel input, rebuilt only when it has moved."""
        if self._pose_rows_version != self.pose_version:
            rows: List[float] = []
            for bone in self.region_set.bones:
                local = self._pose[bone.bone_id]
                position, rotation = local.position, local.rotation
                rows.extend((float(position[0]), float(position[1]),
                             float(position[2]), float(rotation[0]),
                             float(rotation[1]), float(rotation[2]),
                             float(rotation[3])))
            self._pose_rows = tuple(rows)
            self._pose_rows_version = self.pose_version
        return self._pose_rows

    def _surviving_bones(self,
                         limb_state: Optional[Mapping[str, str]]
                         ) -> Optional[List[int]]:
        """Which bone indices still offer a hitbox, or None for all of them.

        **This is where limb state is read, and it is the only place.** The
        kernel below is handed indices and never a limb id, so the L8 boundary
        stays in Python whichever implementation runs (D54). A limb the caller
        reports as severed contributes no capsule; every other state does,
        because whether a disabled arm can still be hit is a gameplay question
        and Scope 5 gives gameplay questions to Octopus's stats module and to
        Shrimp.

        `None` rather than `list(range(n))` when nothing is severed: that is
        the overwhelmingly common case, and it saves building a list per rig
        per volley to say "all of them".
        """
        if not limb_state:
            return None
        keep: List[int] = []
        severed = False
        for index, bone in enumerate(self.region_set.bones):
            if bone.limb_id and not limb_has_hitbox(limb_state.get(bone.limb_id)):
                severed = True
                continue
            keep.append(index)
        return keep if severed else None

    def _world_capsules(self, limb_state: Optional[Mapping[str, str]],
                        place: Optional[Any]) -> Tuple[List[str], Any]:
        """`(regions, packed capsules)` for the bones that survived.

        `place` is the seam kernel. A caller on the hot path passes the one it
        already selected - `select()` costs more than the kernel does for a
        six-bone rig, so looking it up per rig would hand the win straight
        back. A caller that does not care selects here.
        """
        bones = self._surviving_bones(limb_state)
        all_bones = self.region_set.bones
        if bones is None:
            regions = [bone.region for bone in all_bones]
        else:
            regions = [all_bones[i].region for i in bones]
        if place is None:
            from .accel import select as _select
            from .conformance import POSE_CAPSULES as _POSE
            place = _select()[1][_POSE]
        answer = place({"root": {"position": self.root.position,
                                 "rotation": self.root.rotation},
                        "pose": self.pose_rows(),
                        "rest": self.region_set.rest_rows,
                        "bones": bones})
        return regions, answer["capsules"]

    def hitbox_rows(self, *, limb_state: Optional[Mapping[str, str]] = None,
                    place: Optional[Any] = None
                    ) -> List[List[Any]]:
        """`[region, [ax, ay, az], [bx, by, bz], radius]` per live bone.

        The shape `nearest_region` takes, built straight from the kernel's
        bytes. `resolve_volley` used to call `hitboxes()` and immediately take
        the `Capsule`s apart again into exactly this - so the capsules were
        built to be discarded, once per rig per volley.
        """
        regions, packed = self._world_capsules(limb_state, place)
        out: List[List[Any]] = []
        for index, region in enumerate(regions):
            v = struct.unpack_from(_CAPSULE_FORMAT, packed,
                                   index * _CAPSULE_WIDTH)
            out.append([region, [v[0], v[1], v[2]], [v[3], v[4], v[5]], v[6]])
        return out

    def hitboxes(self, *, limb_state: Optional[Mapping[str, str]] = None,
                 place: Optional[Any] = None) -> List[Tuple[str, Capsule]]:
        """(region, capsule) per bone that currently offers a hitbox.

        `limb_state` is passed IN. This module never reads it from Octopus -
        see the module docstring and L8.
        """
        regions, packed = self._world_capsules(limb_state, place)
        out: List[Tuple[str, Capsule]] = []
        for index, region in enumerate(regions):
            v = struct.unpack_from(_CAPSULE_FORMAT, packed,
                                   index * _CAPSULE_WIDTH)
            out.append((region, Capsule((v[0], v[1], v[2]),
                                        (v[3], v[4], v[5]), v[6])))
        return out

    def whole_body_capsule(self) -> Capsule:
        """The capsule that gates a PROJECTILE-tier test (Scope 7).

        **It bounds the rig.** The axis is the rig's full Y span and the radius
        is how far any bone reaches from that axis, radii included - so every
        point of every bone is inside it, for any rig, by construction:
        a point's height is within the span, so the nearest point on the axis is
        directly beside it at exactly its horizontal distance, which is at most
        the radius.

        It used to take its radius from `max(bone.radius)`, which bounds a rig
        only if every bone hugs the vertical axis. A 2.4 m spider got a 0.28 m
        capsule and three quarters of it was unshootable at range while
        remaining hittable in melee - silently, which is the failure mode §13
        invariant 2 names. The humanoid was wrong too, by 17 cm of arm; it went
        unnoticed because the overhang was thin. See DECISIONS.md D19.

        Still derived from the rest extent, not the current pose: a
        PROJECTILE-tier entity is by definition one nobody is animating closely,
        and re-deriving this per frame for every distant NPC is the per-entity
        cost the tiering exists to avoid. The extent is measured once per
        `RegionSet`, so this is O(1).
        """
        extent = self.region_set.extent
        if extent is None:                       # pragma: no cover - defensive
            extent = _rest_extent(self.region_set.bones)
        radius = extent.horizontal_reach or 0.3
        return Capsule(self.root.apply((0.0, extent.min_y, 0.0)),
                       self.root.apply((0.0, extent.max_y, 0.0)), radius)

    def bound_radius(self) -> float:
        """Radius of a sphere at this entity's position containing the whole rig.

        A broad phase measures to an entity's *position*, which is at its feet,
        so this is the margin it has to add or it culls hits before any capsule
        is tested. Derived per rig rather than assumed, because "1.8 m humanoid
        plus margin" is a height argument for a radius and stops being true the
        moment a creature is tall, wide or long (DECISIONS.md D19).
        """
        extent = self.region_set.extent
        if extent is None:                       # pragma: no cover - defensive
            extent = _rest_extent(self.region_set.bones)
        return extent.bound_radius

    def nbytes(self) -> int:
        return 128 + len(self._pose) * 64
