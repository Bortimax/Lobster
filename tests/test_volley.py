"""The volley path (DECISIONS.md D45) — the accelerator seam, finally used.

D26 fixed the seam at *volley* granularity and argued the case in the abstract:
a per-arrow kernel wins the broad phase and hands the win back in dispatch.
This is that argument cashed, and the tests are mostly about the one property
that makes it usable at all:

> **`resolve_volley(shots)` answers exactly what N `resolve_projectile`s
> answer.**

A faster path that answers differently is not a faster path, and nothing about
the speed matters if that fails.
"""

from __future__ import annotations

import random
import unittest

from lobster.accel import select as select_accel
from lobster.budgets import BudgetViolation
from lobster.geometry import Transform
from lobster.hittest import HitTester
from lobster.octopus_bridge import HIT_TEST_ONLY, OctopusBridge
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.spatial import SpatialIndex
from lobster.tiers import ACTIVE, PROJECTILE
from tests.fixtures import VILLAGE, build_session

NO_BUDGET = dict(frame_budget_us=0, max_broad_candidates_per_frame=0,
                 max_capsule_tests_per_frame=0, max_bucket_scans_per_frame=0)


class VolleyFixture(unittest.TestCase):

    def world(self, count=200, seed=3):
        rng = random.Random(seed)
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.index = SpatialIndex(VILLAGE)
        self.skeletons = {}
        for i in range(count):
            entity_id = "e%d" % i
            position = (rng.uniform(0, 128), 0.0, rng.uniform(0, 128))
            self.index.add(entity_id, position,
                           ACTIVE if i % 2 else PROJECTILE)
            self.skeletons[entity_id] = Skeleton(
                entity_id, humanoid_region_set(),
                root=Transform(position=position))

    def shots(self, count=40, seed=11):
        rng = random.Random(seed)
        return [((rng.uniform(0, 128), 1.2, 0.0),
                 (rng.uniform(-0.3, 0.3), 0.0, 1.0), 128.0, 25.0, "archer")
                for _ in range(count)]

    def make_tester(self, bus=None):
        # NOT `tester`: unittest collects anything starting with "test",
        # and a helper called `tester` gets run as a test with no fixture.
        # Second time this project has made that mistake.
        return HitTester(self.index, self.skeletons, bus, **NO_BUDGET)

    @staticmethod
    def digest(results):
        return [[(h.target_id, h.region, round(h.force, 6), h.region_precise,
                  h.snapshot_seq) for h in shot] for shot in results]


class TestItAnswersTheSame(VolleyFixture):
    """The property everything else rests on."""

    def compare(self, count, seed, **kwargs):
        self.world(count, seed=seed)
        volley = self.shots(40, seed=seed + 1)

        one = self.make_tester()
        with self.bridge.frame() as view:
            one.begin_frame()
            separate = [one.resolve_projectile(view, o, d, m, f,
                                               source_id=s, **kwargs)
                        for o, d, m, f, s in volley]

        many = self.make_tester()
        with self.bridge.frame() as view:
            many.begin_frame()
            batched = many.resolve_volley(view, volley, **kwargs)

        self.assertEqual(self.digest(separate), self.digest(batched))
        return separate

    def test_identical_across_several_worlds(self):
        landed = 0
        for count, seed in ((50, 1), (200, 3), (400, 7)):
            landed += sum(len(s) for s in self.compare(count, seed))
        self.assertGreater(landed, 0,
                           "no arrow landed in any world - the comparison is "
                           "between two empty lists")

    def test_identical_when_every_hit_is_wanted(self):
        self.compare(200, 3, first_hit_only=False)

    def test_the_result_is_one_list_per_shot_in_order(self):
        self.world(120)
        volley = self.shots(15)
        tester = self.make_tester()
        with self.bridge.frame() as view:
            tester.begin_frame()
            results = tester.resolve_volley(view, volley)
        self.assertEqual(len(results), len(volley))
        self.assertTrue(all(isinstance(r, list) for r in results))

    def test_it_fires_the_same_events_in_the_same_order(self):
        from lobster.events import EventBus
        self.world(200)
        volley = self.shots(40)

        one_bus, many_bus = EventBus(), EventBus()
        one = HitTester(self.index, self.skeletons, one_bus, **NO_BUDGET)
        with self.bridge.frame() as view:
            one.begin_frame()
            for o, d, m, f, s in volley:
                one.resolve_projectile(view, o, d, m, f, source_id=s)

        many = HitTester(self.index, self.skeletons, many_bus, **NO_BUDGET)
        with self.bridge.frame() as view:
            many.begin_frame()
            many.resolve_volley(view, volley)

        self.assertEqual([e.to_dict() for e in one_bus.log],
                         [e.to_dict() for e in many_bus.log])
        self.assertTrue(one_bus.log, "no events fired at all")


class TestWhatIsSharedAcrossTheVolley(VolleyFixture):
    """Three things a volley can share that separate calls cannot."""

    class CountingView:
        """Wraps a view and counts the `limb_state` reads."""

        def __init__(self, inner):
            self.inner = inner
            self.limb_reads = 0

        def limb_state(self, entity_id, purpose=None):
            self.limb_reads += 1
            return self.inner.limb_state(entity_id, purpose=purpose)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def test_limb_state_is_read_once_per_entity_not_once_per_arrow(self):
        """§13 allows exactly this - *never cached beyond the current
        hit-test or frame* - and a volley is one frame's hit-testing."""
        self.world(200)
        volley = self.shots(40)
        tester = self.make_tester()
        with self.bridge.frame() as inner:
            view = self.CountingView(inner)
            tester.begin_frame()
            tester.resolve_volley(view, volley)
            refinements = tester.stats.bone_refinements

        self.assertGreater(refinements, 0, "nothing was refined")
        self.assertLessEqual(
            view.limb_reads, refinements,
            "limb_state was read more often than rigs were refined - the "
            "per-volley cache is not working")

    def test_a_repeated_target_is_read_once(self):
        """Two arrows into the same person: one read."""
        self.world(1)
        target = self.index.entries()[0]
        straight = ((target.position[0], 1.2, target.position[2] - 20.0),
                    (0.0, 0.0, 1.0), 60.0, 9.0, "archer")
        tester = self.make_tester()
        with self.bridge.frame() as inner:
            view = self.CountingView(inner)
            tester.begin_frame()
            results = tester.resolve_volley(view, [straight] * 5)
        self.assertTrue(any(results), "the fixture never hits")
        self.assertEqual(view.limb_reads, 1)

    def test_the_budget_is_charged_once(self):
        """Not per shot. A volley is one frame's work and one decision about
        whether that frame fits."""
        self.world(200)
        volley = self.shots(30)
        tester = HitTester(self.index, self.skeletons, None)
        with self.bridge.frame() as view:
            tester.begin_frame()
            with self.assertRaises(BudgetViolation) as ctx:
                tester.resolve_volley(view, volley)
        self.assertEqual(ctx.exception.metric, "hit_test_frame_us")
        self.assertEqual(tester.stats.projectiles, len(volley),
                         "the charge happened before every shot was counted")

    def test_a_small_volley_fits_the_budget(self):
        self.world(200)
        tester = HitTester(self.index, self.skeletons, None)
        with self.bridge.frame() as view:
            tester.begin_frame()
            tester.resolve_volley(view, self.shots(4))


class TestItRunsOnEveryImplementation(VolleyFixture):
    """The seam's whole point: same answers on C, on NumPy, on neither."""

    def test_every_available_accelerator_agrees(self):
        from lobster.accel import available
        self.world(200)
        volley = self.shots(30)

        baseline = None
        for entry in available():
            if not entry["available"]:
                continue
            name = entry["name"]
            with self.subTest(accelerator=name):
                import lobster.accel as accel
                original = accel.KERNEL_PREFERENCE
                accel.KERNEL_PREFERENCE = {k: (name, "python")
                                           for k in original}
                try:
                    tester = self.make_tester()
                    with self.bridge.frame() as view:
                        tester.begin_frame()
                        got = self.digest(tester.resolve_volley(view, volley))
                finally:
                    accel.KERNEL_PREFERENCE = original
                if baseline is None:
                    baseline = got
                else:
                    self.assertEqual(got, baseline,
                                     "{0} disagrees with the others".format(name))
        self.assertIsNotNone(baseline)


class TestWhatItRefusesToChange(VolleyFixture):
    """A volley must not quietly become a different kind of hit-test."""

    def test_a_rigless_candidate_still_raises(self):
        """`_require_rig` is the loud failure D16 added; batching must not
        turn it into a silent skip."""
        from lobster.hittest import HitTestError
        self.world(1)
        entry = self.index.entries()[0]
        self.skeletons.clear()
        shot = ((entry.position[0], 1.2, entry.position[2] - 10.0),
                (0.0, 0.0, 1.0), 40.0, 9.0, "archer")
        tester = self.make_tester()
        with self.bridge.frame() as view:
            tester.begin_frame()
            with self.assertRaises(HitTestError):
                tester.resolve_volley(view, [shot])

    def test_dormant_entities_are_still_not_candidates(self):
        """Scope §7: PROJECTILE is a hit-test tier, DORMANT has no hitbox."""
        from lobster.tiers import DORMANT
        self.world(0)
        self.index.add("sleeper", (10.0, 0.0, 10.0), DORMANT)
        shot = ((10.0, 1.2, 0.0), (0.0, 0.0, 1.0), 40.0, 9.0, "archer")
        tester = self.make_tester()
        with self.bridge.frame() as view:
            tester.begin_frame()
            self.assertEqual(tester.resolve_volley(view, [shot]), [[]])

    def test_an_empty_volley_costs_nothing(self):
        self.world(50)
        tester = self.make_tester()
        with self.bridge.frame() as view:
            tester.begin_frame()
            self.assertEqual(tester.resolve_volley(view, []), [])
        self.assertEqual(tester.stats.projectiles, 0)


if __name__ == "__main__":
    unittest.main()
