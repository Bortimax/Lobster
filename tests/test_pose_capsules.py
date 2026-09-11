"""The fourth seam kernel, wired (DECISIONS.md D54).

`Skeleton.hitboxes` was ~61% of a volley. The tests that matter are the ones
that would catch it having become *faster and wrong*:

1. **The answer did not move** - on every implementation, and against the
   `capsule_for` it replaced.
2. **The pose cache cannot go stale.** It is keyed on `pose_version`, and a rig
   that reports last frame's bones is a hit registered on a body that has moved.
3. **Limb state never reaches the kernel.** Which bones survive is decided in
   Python and handed over as indices - the L8 boundary, made structural rather
   than promised.

The kernel's own correctness is the differential harness's job
(`test_conformance.py`), over thousands of generated cases.
"""

from __future__ import annotations

import math
import random
import struct
import unittest

from lobster import accel
from lobster.conformance import CAPSULE_FLOATS, CAPSULE_PACK, POSE_CAPSULES
from lobster.geometry import Capsule, Transform
from lobster.skeleton import (Bone, RegionSet, Skeleton, _CAPSULE_FORMAT,
                              _CAPSULE_WIDTH, humanoid_region_set)


def spin(rng):
    axis = [rng.uniform(-1, 1) for _ in range(3)]
    norm = math.sqrt(sum(c * c for c in axis)) or 1.0
    angle = rng.uniform(-math.pi, math.pi)
    k = math.sin(angle / 2.0) / norm
    return (axis[0] * k, axis[1] * k, axis[2] * k, math.cos(angle / 2.0))


def posed(seed=1, position=(3.0, 0.0, 4.0)):
    rng = random.Random(seed)
    region_set = humanoid_region_set()
    skeleton = Skeleton("e", region_set,
                        root=Transform(position=position, rotation=spin(rng)))
    skeleton.set_pose({b.bone_id: Transform(
        position=(rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2),
                  rng.uniform(-0.2, 0.2)), rotation=spin(rng))
        for b in region_set.bones})
    return skeleton


def implementations():
    return [e["name"] for e in accel.available() if e["available"]]


class Using:
    """Force one implementation of the pose kernel for a block."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.original = accel.KERNEL_PREFERENCE
        accel.KERNEL_PREFERENCE = dict(self.original,
                                       **{POSE_CAPSULES: (self.name,)})
        return self

    def __exit__(self, *exc):
        accel.KERNEL_PREFERENCE = self.original


# ---------------------------------------------------------------------------
# 1. The answer did not move
# ---------------------------------------------------------------------------

class TestTheAnswerIsUnchanged(unittest.TestCase):

    def test_it_matches_capsule_for_bone_by_bone(self):
        """`capsule_for` is the definition this kernel replaces in bulk. If
        they disagreed, one of the two callers of each would be wrong."""
        skeleton = posed()
        for region, capsule in skeleton.hitboxes():
            pass
        for bone, (region, capsule) in zip(skeleton.region_set.bones,
                                           skeleton.hitboxes()):
            self.assertEqual(region, bone.region)
            one = skeleton.capsule_for(bone.bone_id)
            for got, want in zip(capsule.a, one.a):
                self.assertAlmostEqual(got, want, places=9)
            for got, want in zip(capsule.b, one.b):
                self.assertAlmostEqual(got, want, places=9)
            self.assertAlmostEqual(capsule.radius, one.radius, places=12)

    def test_every_implementation_agrees(self):
        skeleton = posed(seed=4)
        baseline = None
        for name in implementations():
            with self.subTest(implementation=name), Using(name):
                got = [(r, tuple(round(v, 9) for v in c.a),
                        tuple(round(v, 9) for v in c.b), round(c.radius, 12))
                       for r, c in skeleton.hitboxes()]
            if baseline is None:
                baseline = got
                self.assertTrue(baseline, "no capsules at all")
            else:
                self.assertEqual(got, baseline,
                                 "{0} places the rig differently".format(name))

    def test_hitbox_rows_and_hitboxes_describe_the_same_capsules(self):
        """One builds `Capsule`s and one does not; they must not diverge, since
        the volley uses the second and selection the first."""
        skeleton = posed(seed=7)
        rows = skeleton.hitbox_rows()
        boxes = skeleton.hitboxes()
        self.assertEqual(len(rows), len(boxes))
        for (region, a, b, radius), (r2, capsule) in zip(rows, boxes):
            self.assertEqual(region, r2)
            self.assertEqual(tuple(a), capsule.a)
            self.assertEqual(tuple(b), capsule.b)
            self.assertEqual(radius, capsule.radius)

    def test_a_rig_at_rest_sits_where_its_root_is(self):
        """Every bone within the rig's own declared reach of the root - not
        *at* the root, which was this test's first draft: a humanoid's arms and
        legs are offset in x, so only the head and torso sit on the axis."""
        skeleton = Skeleton("e", humanoid_region_set(),
                            root=Transform(position=(10.0, 0.0, -4.0)))
        reach = skeleton.region_set.extent.horizontal_reach
        self.assertGreater(reach, 0.0)
        for _region, capsule in skeleton.hitboxes():
            for point in (capsule.a, capsule.b):
                self.assertLessEqual(
                    math.hypot(point[0] - 10.0, point[2] + 4.0), reach)
        head = dict(skeleton.hitboxes())["head"]
        self.assertAlmostEqual(head.a[0], 10.0, places=9)
        self.assertAlmostEqual(head.a[2], -4.0, places=9)


# ---------------------------------------------------------------------------
# 2. The cache
# ---------------------------------------------------------------------------

class TestThePoseCacheCannotGoStale(unittest.TestCase):
    """A rig that reports last frame's bones is a hit on a body that moved."""

    def test_setting_a_pose_moves_the_capsules(self):
        skeleton = posed(seed=2)
        before = skeleton.hitboxes()
        bone_id = skeleton.region_set.bones[0].bone_id
        skeleton.set_pose({bone_id: Transform(position=(0.0, 5.0, 0.0))})
        after = skeleton.hitboxes()
        self.assertNotEqual(before[0][1].a, after[0][1].a,
                            "the pose changed and the capsule did not")

    def test_setting_the_root_moves_the_capsules(self):
        skeleton = posed(seed=3)
        before = skeleton.hitboxes()
        skeleton.set_root(Transform(position=(100.0, 0.0, 100.0)))
        after = skeleton.hitboxes()
        self.assertNotEqual(before[0][1].a, after[0][1].a)

    def test_the_rows_are_rebuilt_exactly_when_the_version_moves(self):
        skeleton = posed(seed=5)
        first = skeleton.pose_rows()
        self.assertIs(skeleton.pose_rows(), first, "rebuilt without a write")
        skeleton.set_pose({})
        self.assertIsNot(skeleton.pose_rows(), first,
                         "a write did not invalidate the rows")

    def test_the_root_is_not_cached_at_all(self):
        """`set_root` bumps the version too, but the root is read fresh on
        every call anyway - it is one transform, not a per-bone array."""
        skeleton = posed(seed=6)
        rows = skeleton.pose_rows()
        skeleton.set_root(Transform(position=(1.0, 2.0, 3.0)))
        self.assertEqual(len(skeleton.pose_rows()), len(rows))

    def test_the_rest_rows_are_built_once_per_region_set(self):
        region_set = humanoid_region_set()
        self.assertIs(region_set.rest_rows, region_set.rest_rows)
        self.assertEqual(len(region_set.rest_rows),
                         len(region_set.bones) * CAPSULE_FLOATS)

    def test_two_skeletons_share_one_region_set_s_rest_rows(self):
        region_set = humanoid_region_set()
        a = Skeleton("a", region_set)
        b = Skeleton("b", region_set,
                     root=Transform(position=(9.0, 0.0, 9.0)))
        self.assertIs(a.region_set.rest_rows, b.region_set.rest_rows)
        self.assertNotEqual(a.hitboxes()[0][1].a, b.hitboxes()[0][1].a)


# ---------------------------------------------------------------------------
# 3. The L8 boundary
# ---------------------------------------------------------------------------

class TestLimbStateNeverReachesTheKernel(unittest.TestCase):
    """Which bones offer a hitbox is policy; where they are is arithmetic."""

    def limbed(self):
        skeleton = posed(seed=8)
        limbs = [b.limb_id for b in skeleton.region_set.bones if b.limb_id]
        self.assertTrue(limbs, "the humanoid must have severable limbs")
        return skeleton, limbs

    def test_a_severed_limb_contributes_no_capsule(self):
        skeleton, limbs = self.limbed()
        whole = skeleton.hitboxes()
        maimed = skeleton.hitboxes(limb_state={limbs[0]: "severed"})
        self.assertLess(len(maimed), len(whole))

    def test_the_survivors_keep_their_regions_and_order(self):
        skeleton, limbs = self.limbed()
        whole = skeleton.hitboxes()
        maimed = skeleton.hitboxes(limb_state={limbs[0]: "severed"})
        gone = {b.region for b in skeleton.region_set.bones
                if b.limb_id == limbs[0]}
        self.assertEqual([r for r, _c in maimed],
                         [r for r, _c in whole if r not in gone])

    def test_a_surviving_bone_is_placed_identically(self):
        """The filter must not perturb the arithmetic for the bones that
        remain - a rig with an arm off is not a rig in a different place."""
        skeleton, limbs = self.limbed()
        whole = dict(skeleton.hitboxes())
        maimed = dict(skeleton.hitboxes(limb_state={limbs[0]: "severed"}))
        for region, capsule in maimed.items():
            self.assertEqual(capsule.a, whole[region].a)
            self.assertEqual(capsule.b, whole[region].b)

    def test_every_limb_severed_leaves_only_the_unsevered_bones(self):
        skeleton, limbs = self.limbed()
        state = {limb: "severed" for limb in limbs}
        left = skeleton.hitboxes(limb_state=state)
        expected = [b.region for b in skeleton.region_set.bones
                    if not b.limb_id]
        self.assertEqual([r for r, _c in left], expected)

    def test_a_state_that_is_not_severed_keeps_its_hitbox(self):
        """Scope 5: whether a *disabled* arm can still be hit is a gameplay
        question, and gameplay questions are not Lobster's."""
        skeleton, limbs = self.limbed()
        self.assertEqual(len(skeleton.hitboxes(limb_state={limbs[0]: "broken"})),
                         len(skeleton.hitboxes()))

    def test_the_payload_carries_indices_and_nothing_about_limbs(self):
        """The structural half of L8: whatever the kernel is, it cannot read
        limb state, because limb state is not in what it is given."""
        seen = {}

        def spy(payload):
            seen.update(payload)
            from lobster.conformance import reference_pose_capsules
            return reference_pose_capsules(payload)

        skeleton, limbs = self.limbed()
        skeleton.hitboxes(limb_state={limbs[0]: "severed"}, place=spy)
        self.assertEqual(sorted(seen), ["bones", "pose", "rest", "root"])
        self.assertTrue(all(isinstance(i, int) for i in seen["bones"]))
        flat = repr(seen)
        for limb in limbs:
            self.assertNotIn(limb, flat)
        self.assertNotIn("severed", flat)

    def test_an_unsevered_rig_sends_no_index_list_at_all(self):
        """The common case, and the reason `bones` may be None: building a list
        that says "all of them" per rig per volley is the cost this kernel
        exists to remove."""
        seen = {}

        def spy(payload):
            seen.update(payload)
            from lobster.conformance import reference_pose_capsules
            return reference_pose_capsules(payload)

        posed().hitboxes(place=spy)
        self.assertIsNone(seen["bones"])


# ---------------------------------------------------------------------------
# The wire format, spelled in two files
# ---------------------------------------------------------------------------

class TestTheWireFormatAgrees(unittest.TestCase):

    def test_the_skeleton_and_the_seam_describe_the_same_bytes(self):
        self.assertEqual(_CAPSULE_FORMAT,
                         "%d%s" % (CAPSULE_FLOATS, CAPSULE_PACK))
        self.assertEqual(_CAPSULE_WIDTH,
                         struct.calcsize("%d%s" % (CAPSULE_FLOATS,
                                                   CAPSULE_PACK)))

    def test_it_is_float64_and_not_float32(self):
        """Its consumer is `nearest_region`, which works in doubles. Narrowing
        between two CPU kernels would throw away precision to save nothing -
        `place_batch` narrows because a GPU buffer is on the other end."""
        self.assertEqual(CAPSULE_PACK, "d")
        self.assertEqual(_CAPSULE_WIDTH, CAPSULE_FLOATS * 8)

    def test_a_bone_index_this_rig_does_not_have_is_refused(self):
        """Loud on every implementation, and negative indices too: Python and
        numpy would both have silently served the last bone for -1."""
        region_set = humanoid_region_set()
        payload = {"root": {"position": (0.0, 0.0, 0.0),
                            "rotation": (0.0, 0.0, 0.0, 1.0)},
                   "pose": Skeleton("e", region_set).pose_rows(),
                   "rest": region_set.rest_rows,
                   "bones": [len(region_set.bones)]}
        for name in implementations():
            with self.subTest(implementation=name):
                kernel = accel.select(prefer=name)[1][POSE_CAPSULES]
                with self.assertRaises(IndexError):
                    kernel(payload)
                with self.assertRaises(IndexError):
                    kernel(dict(payload, bones=[-1]))


# ---------------------------------------------------------------------------
# A rig that is not the shipped humanoid
# ---------------------------------------------------------------------------

class TestAnUnusualRig(unittest.TestCase):
    """"non-humanoid creatures get their own declared region sets at
    content-time" - so the kernel must not assume six bones or a humanoid."""

    def rig(self, count):
        bones = tuple(
            Bone(bone_id="b%d" % i, region="segment", a=(0.0, i * 0.5, 0.0),
                 b=(0.0, i * 0.5 + 0.4, 0.0), radius=0.1 + i * 0.01,
                 limb_id="seg%d" % i if i % 3 == 0 else None)
            for i in range(count))
        return RegionSet(name="worm", regions=("segment",), bones=bones)

    def test_a_forty_bone_rig(self):
        skeleton = Skeleton("w", self.rig(40),
                            root=Transform(position=(2.0, 0.0, 3.0)))
        boxes = skeleton.hitboxes()
        self.assertEqual(len(boxes), 40)
        for bone, (_region, capsule) in zip(skeleton.region_set.bones, boxes):
            self.assertAlmostEqual(capsule.radius, bone.radius, places=12)

    def test_a_one_bone_rig(self):
        self.assertEqual(len(Skeleton("w", self.rig(1)).hitboxes()), 1)

    def test_a_rig_with_no_bones_at_all(self):
        empty = RegionSet(name="ghost", regions=("segment",), bones=())
        self.assertEqual(Skeleton("g", empty).hitboxes(), [])
        self.assertEqual(Skeleton("g", empty).hitbox_rows(), [])


if __name__ == "__main__":
    unittest.main()


class TestBoneMatricesSayWhereTheBoneIs(unittest.TestCase):
    """`bone_matrices` and `capsule_for` are two answers to one question, and
    the docstring on the first says *world space*. A renderer that draws a
    model per bone reads the first; a shot that resolves to a limb reads the
    second. They have to be the same composition.
    """

    def placed(self, skeleton, bone):
        """Where the bone's rest capsule ends up, via `bone_matrices`."""
        transform = skeleton.bone_matrices()[bone.bone_id]
        return transform.apply(bone.a), transform.apply(bone.b)

    def test_it_places_every_bone_where_the_capsule_is(self):
        skeleton = posed(seed=7)
        for bone in skeleton.region_set.bones:
            with self.subTest(bone=bone.bone_id):
                capsule = skeleton.capsule_for(bone.bone_id)
                a, b = self.placed(skeleton, bone)
                for got, want in zip(a, capsule.a):
                    self.assertAlmostEqual(got, want, places=9)
                for got, want in zip(b, capsule.b):
                    self.assertAlmostEqual(got, want, places=9)

    def test_the_rig_has_a_bone_off_the_vertical_axis(self):
        """The guard on the test above. A head and a torso sit on the axis the
        root turns about, so they land in the same place whether or not the
        root's rotation is composed in - an arm does not. Without an offset
        bone the assertion holds against a rig that is only half-placed.
        """
        offset = [b.bone_id for b in humanoid_region_set().bones
                  if abs(b.a[0]) > 1e-6 or abs(b.a[2]) > 1e-6]
        self.assertTrue(offset,
                        "every bone is on the vertical axis, so dropping the "
                        "root's rotation would be invisible here")

    def test_turning_an_entity_turns_its_bones(self):
        """The failure in the form somebody would see it: a character facing
        east with its arms still pointing north."""
        region_set = humanoid_region_set()
        arm = next(b for b in region_set.bones
                   if abs(b.a[0]) > 1e-6 or abs(b.a[2]) > 1e-6)
        half = math.pi / 4.0
        facing = Transform(position=(0.0, 0.0, 0.0),
                           rotation=(0.0, math.sin(half), 0.0, math.cos(half)))

        rest = Skeleton("rest", region_set, root=Transform())
        turned = Skeleton("turned", region_set, root=facing)

        still = rest.bone_matrices()[arm.bone_id].apply(arm.a)
        moved = turned.bone_matrices()[arm.bone_id].apply(arm.a)
        self.assertGreater(
            sum((moved[i] - still[i]) ** 2 for i in range(3)) ** 0.5, 0.1,
            "turning the entity did not move its arm")
        self.assertAlmostEqual(moved[0], still[2], places=9)
        self.assertAlmostEqual(moved[2], -still[0], places=9)

    def test_the_root_alone_still_moves_a_bone(self):
        """The other half: translation was never the broken part, and a fix
        that composed the rotation and dropped the offset would pass the tests
        above."""
        region_set = humanoid_region_set()
        here = Skeleton("here", region_set, root=Transform())
        there = Skeleton("there", region_set,
                         root=Transform(position=(10.0, 2.0, -4.0)))
        for bone in region_set.bones:
            with self.subTest(bone=bone.bone_id):
                a = here.bone_matrices()[bone.bone_id].apply(bone.a)
                b = there.bone_matrices()[bone.bone_id].apply(bone.a)
                self.assertAlmostEqual(b[0] - a[0], 10.0, places=9)
                self.assertAlmostEqual(b[1] - a[1], 2.0, places=9)
                self.assertAlmostEqual(b[2] - a[2], -4.0, places=9)
