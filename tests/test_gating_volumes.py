"""Gating volumes must be derived from the rig, not assumed (Shrimp #4, #5).

Two findings from Shrimp's first real content build, one root cause: a volume
that gates a hit test was built without reference to how much space the thing
being tested actually occupies.

* `whole_body_capsule` took its radius from `max(bone.radius)`, which bounds a
  rig only if every bone hugs the vertical axis. A 2.4 m spider got a 0.28 m
  capsule and was unshootable at range while remaining hittable in melee.
* `resolve_swing` culled on distance to an entity's *position*, which is at its
  feet, so melee only connected if you swung at people's ankles.

Both failed silently, which CONTRACT §5 invariant 2 names as the worst way for
an integration mistake to present. These tests are the ones that would have
caught them; the property assertions matter more than the scenarios, because a
future rig will not look like either of these.

See DECISIONS.md D19.
"""

from __future__ import annotations

import math
import unittest

from lobster.constants import PROJECTILE_BROAD_RADIUS_M
from lobster.geometry import Capsule, Transform
from lobster.hittest import HitTester
from lobster.octopus_bridge import OctopusBridge
from lobster.skeleton import (Bone, RegionSet, Skeleton, humanoid_region_set)
from lobster.spatial import SpatialIndex
from lobster.tiers import ACTIVE, PROJECTILE
from tests.fixtures import VILLAGE, build_session


def fen_spider() -> RegionSet:
    """Shrimp's probe creature: eight legs, 2.4 m tip to tip, 0.53 m tall."""
    bones = [Bone("cephalothorax", "core", (0, 0.25, 0), (0, 0.45, 0), 0.22),
             Bone("abdomen", "abdomen", (0, 0.25, -0.35), (0, 0.40, -0.55), 0.28)]
    for i in range(8):
        angle = 2 * math.pi * i / 8
        bones.append(Bone(
            "leg_{0}".format(i), "leg_{0}".format(i),
            (0.22 * math.sin(angle), 0.30, 0.22 * math.cos(angle)),
            (1.20 * math.sin(angle), 0.02, 1.20 * math.cos(angle)), 0.05))
    return RegionSet("fen_spider", tuple(sorted({b.region for b in bones})),
                     tuple(bones))


def wyrm() -> RegionSet:
    """Six metres tall - taller than the old broad-phase constant."""
    bones = (Bone("head", "head", (0, 5.4, 0), (0, 6.0, 0), 0.5),
             Bone("neck", "neck", (0, 3.6, 0), (0, 5.4, 0), 0.6),
             Bone("body", "body", (0, 1.2, 0), (0, 3.6, 0), 1.1),
             Bone("tail", "tail", (0, 0.4, 0), (0, 1.2, 0), 0.7))
    return RegionSet("wyrm", tuple(sorted({b.region for b in bones})), bones)


def surface_points(region_set: RegionSet):
    """Sample points on each bone's capsule surface, in rest space."""
    for bone in region_set.bones:
        for point in (bone.a, bone.b):
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                               (0, 0, 1), (0, 0, -1)):
                yield (point[0] + dx * bone.radius,
                       point[1] + dy * bone.radius,
                       point[2] + dz * bone.radius)


class TestTheBodyCapsuleBoundsTheRig(unittest.TestCase):
    """Shrimp finding #4."""

    RIGS = (("humanoid", humanoid_region_set()),
            ("fen_spider", fen_spider()),
            ("wyrm", wyrm()))

    def test_every_rig_is_inside_its_own_body_capsule(self):
        """The property. A gate that does not contain the thing it gates is
        not a gate - it is a silent filter."""
        for name, region_set in self.RIGS:
            with self.subTest(rig=name):
                skeleton = Skeleton("x", region_set,
                                    root=Transform(position=(3.0, 0.0, 7.0)))
                capsule = skeleton.whole_body_capsule()
                outside = [p for p in surface_points(region_set)
                           if not capsule.contains(
                               skeleton.root.apply(p))]
                self.assertEqual(
                    outside, [],
                    "{0}: {1} point(s) of the rig fall outside the capsule that "
                    "decides whether it was hit".format(name, len(outside)))

    def test_the_humanoid_was_wrong_too_just_subclinically(self):
        """max(bone.radius) is 0.198; the arms reach 0.369."""
        region_set = humanoid_region_set()
        widest_bone = max(b.radius for b in region_set.bones)
        self.assertGreater(region_set.extent.horizontal_reach, widest_bone,
                           "if these were equal the old implementation would "
                           "have been right by luck, and this test proves "
                           "nothing")
        skeleton = Skeleton("npc-ada", region_set)
        self.assertAlmostEqual(skeleton.whole_body_capsule().radius,
                               region_set.extent.horizontal_reach, places=9)

    def test_a_wide_rig_gets_a_wide_capsule(self):
        spider = fen_spider()
        capsule = Skeleton("spider", spider).whole_body_capsule()
        self.assertGreater(capsule.radius, 1.2,
                           "a 2.4 m creature needs more than a 0.28 m gate")

    def test_a_sniper_resolves_the_leg_a_swordsman_can_reach(self):
        """CONTRACT §1: "a sniper's shot two cells away resolves to a limb
        exactly as a sword blow does." The same ray, both tiers, same answer."""
        session = build_session()
        bridge = OctopusBridge(session)
        spider = fen_spider()
        position = (20.0, 0.0, 10.0)
        results = {}
        for tier in (ACTIVE, PROJECTILE):
            index = SpatialIndex(VILLAGE)
            index.snapshot([("spider", position, tier)])
            skeletons = {"spider": Skeleton("spider", spider,
                                            root=Transform(position=position))}
            tester = HitTester(index, skeletons)
            with bridge.frame() as view:
                hits = tester.resolve_projectile(
                    view, (0.0, 0.05, 10.85), (1.0, 0.0, 0.0), 80.0, 9.0)
            results[tier] = hits[0].region if hits else None

        self.assertIsNotNone(results[ACTIVE], "the melee case must land")
        self.assertEqual(results[PROJECTILE], results[ACTIVE],
                         "the same ray must resolve the same leg at range")


class TestTheSwingBroadPhaseReachesTheWholeBody(unittest.TestCase):
    """Shrimp finding #5."""

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.position = (4.0, 0.0, 4.0)
        self.index = SpatialIndex(VILLAGE)
        self.index.snapshot([("npc-ada", self.position, ACTIVE)])
        self.skeletons = {"npc-ada": Skeleton(
            "npc-ada", humanoid_region_set(),
            root=Transform(position=self.position))}

    def swing_at(self, height: float):
        tester = HitTester(self.index, self.skeletons)
        tester.begin_frame()
        blade = Capsule((3.4, height, 4.0), (4.4, height, 4.0), 0.15)
        with self.bridge.frame() as view:
            hits = tester.resolve_swing(view, blade, 10.0, source_id="player")
        return (hits[0].region if hits else None), tester.stats.capsule_tests

    def test_a_swordsman_can_swing_at_a_head(self):
        region, tests = self.swing_at(1.7)
        self.assertEqual(region, "head")
        self.assertGreater(tests, 0)

    def test_melee_connects_at_every_plausible_height(self):
        """It used to work only at 0.2 m, and with zero capsule tests - the
        entity never even became a candidate."""
        for height in (0.2, 0.9, 1.2, 1.7):
            with self.subTest(height=height):
                region, tests = self.swing_at(height)
                self.assertIsNotNone(
                    region,
                    "a swing at {0} m found nobody standing right there".format(
                        height))
                self.assertGreater(tests, 0)

    def test_a_swing_at_nobody_still_misses(self):
        """Widening the broad phase must not turn it into a hit-everything."""
        tester = HitTester(self.index, self.skeletons)
        blade = Capsule((40.0, 1.2, 40.0), (41.0, 1.2, 40.0), 0.15)
        with self.bridge.frame() as view:
            self.assertEqual(tester.resolve_swing(view, blade, 10.0), [])


class TestTheBroadMarginIsDerived(unittest.TestCase):
    """The constant was a height argument for a radius; now it is a floor."""

    def make(self, region_set):
        index = SpatialIndex(VILLAGE)
        index.snapshot([("x", (30.0, 0.0, 10.0), PROJECTILE)])
        skeletons = {"x": Skeleton("x", region_set,
                                   root=Transform(position=(30.0, 0.0, 10.0)))}
        return HitTester(index, skeletons), skeletons

    def test_a_humanoid_uses_the_declared_floor(self):
        tester, _ = self.make(humanoid_region_set())
        self.assertEqual(tester.broad_margin(), PROJECTILE_BROAD_RADIUS_M)

    def test_a_tall_rig_raises_the_margin_above_the_constant(self):
        tester, skeletons = self.make(wyrm())
        self.assertGreater(skeletons["x"].bound_radius(),
                           PROJECTILE_BROAD_RADIUS_M)
        self.assertEqual(tester.broad_margin(), skeletons["x"].bound_radius())

    def test_a_six_metre_wyrm_can_be_shot_in_the_head(self):
        """The case the fixed constant would have culled at broad phase."""
        session = build_session()
        bridge = OctopusBridge(session)
        tester, _ = self.make(wyrm())
        with bridge.frame() as view:
            hits = tester.resolve_projectile(view, (0.0, 5.7, 10.0),
                                             (1.0, 0.0, 0.0), 80.0, 20.0)
        self.assertEqual([h.region for h in hits], ["head"])

    def test_the_margin_follows_the_rigs_that_are_registered(self):
        index = SpatialIndex(VILLAGE)
        index.snapshot([("a", (10.0, 0.0, 10.0), PROJECTILE)])
        skeletons = {"a": Skeleton("a", humanoid_region_set())}
        tester = HitTester(index, skeletons)
        self.assertEqual(tester.broad_margin(), PROJECTILE_BROAD_RADIUS_M)
        skeletons["b"] = Skeleton("b", wyrm())
        self.assertGreater(tester.broad_margin(), PROJECTILE_BROAD_RADIUS_M,
                           "adding a taller rig must widen the margin, or its "
                           "hits are culled before any capsule is tested")


if __name__ == "__main__":
    unittest.main()
