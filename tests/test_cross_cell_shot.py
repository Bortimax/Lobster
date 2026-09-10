"""A shot crosses the cell boundary the contract says it crosses (Shrimp #8).

CONTRACT §3 defines PROJECTILE as "in whatever cell", Scope §7 as "regardless of
connection-graph adjacency", and CONTRACT §1 promises a sniper's shot two cells
away resolves to a limb. `HitTester` is built around one `ResidentCell.index`, so
none of it was true: a bandit 16 m away across the boundary was on screen, inside
a 45 m shot, and unhittable.

Placement (D21) is what made it visible rather than theoretical — you can now see
the next valley. `WorldHitTester` is the answer, and DECISIONS.md D23 records the
shape: the ray moves into each cell's space, the world does not move into one.
"""

from __future__ import annotations

import unittest

from lobster.cell import CellManager
from lobster.events import EventBus
from lobster.geometry import Transform, distance, normalize
from lobster.hittest import HitTester, HitTestError, WorldHitTester
from lobster.octopus_bridge import OctopusBridge
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.tiers import ACTIVE, DORMANT, PROJECTILE
from tests.fixtures import FIELD, VILLAGE, build_session, standard_workspace

#: just short of the village's north edge, looking into the field beyond
SHOOTER = (60.0, 1.0, 100.0)
NORTH = (0.0, 0.0, 1.0)


class CrossCellFixture(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.manager = CellManager(self.ws.path)
        self.view = self.bridge.frame()
        self.manager.set_player_cell(self.view, VILLAGE)
        self.placements = self.manager.placements(self.view)

    def stand(self, cell_id: str, entity_id: str, local, tier=PROJECTILE):
        cell = self.manager.resident[cell_id]
        cell.place(entity_id, local, tier,
                   skeleton=Skeleton(entity_id, humanoid_region_set(),
                                     root=Transform(position=local)))
        return self.placements[cell_id].apply(local)

    def world_tester(self, bus=None):
        return WorldHitTester.from_manager(self.manager, self.view, bus)


class TestTheShotCrossesTheBoundary(CrossCellFixture):

    def test_the_bandit_across_the_boundary_can_be_hit(self):
        world = self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        self.assertLess(distance(SHOOTER, world), 45.0,
                        "the fixture must put him inside the shot's range")

        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 45.0, 20.0, source_id="player")
        self.assertEqual([h.target_id for h in hits], ["bandit"])
        self.assertEqual(hits[0].cell_id, FIELD,
                         "a cross-cell hit must say which cell it was in")

    def test_the_old_single_cell_tester_still_cannot(self):
        """The gap, pinned. If this ever passes, the two paths have converged
        and `WorldHitTester` is redundant rather than load-bearing."""
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        village = self.manager.resident[VILLAGE]
        single = HitTester(village.index, village.skeletons)
        self.assertEqual(
            single.resolve_projectile(self.view, SHOOTER, NORTH, 45.0, 20.0), [])

    def test_it_resolves_a_limb_not_just_a_body(self):
        """CONTRACT §1: "resolves to a limb exactly as a sword blow does"."""
        from lobster.skeleton import HUMANOID_REGIONS
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 45.0, 20.0, source_id="player")
        self.assertIn(hits[0].region, HUMANOID_REGIONS)

    def test_out_of_range_is_still_out_of_range(self):
        """Crossing cells must not mean crossing them for free."""
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        self.assertEqual(
            self.world_tester().resolve_projectile(
                self.view, SHOOTER, NORTH, 20.0, 20.0), [],
            "a 20 m shot must not reach a target 36 m away")


class TestOrderingAcrossCells(CrossCellFixture):

    def setUp(self):
        super().setUp()
        # the NEAR target is in cell-village, the FAR one in cell-field - and
        # 'cell-field' sorts first, so an implementation that returned whichever
        # cell it asked first would return the far one
        self.far = self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        self.near = self.stand(VILLAGE, "guard", (60.0, 0.0, 118.0), ACTIVE)

    def test_hits_come_back_nearest_first(self):
        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 60.0, 20.0, first_hit_only=False)
        self.assertEqual([h.target_id for h in hits], ["guard", "bandit"])
        ranges = [h.distance_from_source for h in hits]
        self.assertEqual(ranges, sorted(ranges))

    def test_first_hit_is_decided_after_every_cell_answers(self):
        """Not 'whichever cell was asked first' - the shot stops in the guard."""
        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 60.0, 20.0, first_hit_only=True)
        self.assertEqual([h.target_id for h in hits], ["guard"])

    def test_shooting_past_the_near_target_reaches_the_far_one(self):
        hits = self.world_tester().resolve_projectile(
            self.view, (60.0, 1.0, 125.0), NORTH, 45.0, 20.0)
        self.assertEqual([(h.target_id, h.cell_id) for h in hits],
                         [("bandit", FIELD)])

    def test_range_is_measured_in_world_space(self):
        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 60.0, 20.0, first_hit_only=False)
        by_id = {h.target_id: h for h in hits}
        self.assertAlmostEqual(by_id["bandit"].distance_from_source,
                               distance(SHOOTER, self.far), places=5)
        self.assertAlmostEqual(by_id["guard"].distance_from_source,
                               distance(SHOOTER, self.near), places=5)


class TestReportingMatchesTheResult(CrossCellFixture):
    """A discarded hit must not have fired an Event on its way out."""

    def setUp(self):
        super().setUp()
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        self.stand(VILLAGE, "guard", (60.0, 0.0, 118.0), ACTIVE)

    def fire(self, first_hit_only):
        bus = EventBus()
        hits = self.world_tester(bus).resolve_projectile(
            self.view, SHOOTER, NORTH, 60.0, 20.0, source_id="player",
            first_hit_only=first_hit_only)
        return ([h.target_id for h in hits],
                [e.target_id for e in bus.events_of("on_hit_location")])

    def test_one_hit_fires_one_event(self):
        returned, fired = self.fire(True)
        self.assertEqual(returned, ["guard"])
        self.assertEqual(fired, ["guard"],
                         "the bandit the shot never reached must not be "
                         "reported as hit")

    def test_every_returned_hit_fires(self):
        returned, fired = self.fire(False)
        self.assertEqual(sorted(returned), sorted(fired))


class TestWhatIsDeliberatelyUnchanged(CrossCellFixture):

    def test_melee_stays_in_one_cell(self):
        """ACTIVE is "current cell + immediate melee range" - a sword does not
        reach the next cell, so this is a definition, not an omission."""
        self.assertFalse(hasattr(WorldHitTester, "resolve_swing"))
        self.assertFalse(hasattr(WorldHitTester, "resolve_blast"))

    def test_no_tier_is_promoted_by_being_shot_from_another_cell(self):
        from lobster.tiers import octopus_tier_for
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        tester = self.world_tester()
        tester.resolve_projectile(self.view, SHOOTER, NORTH, 45.0, 20.0)
        entry = self.manager.resident[FIELD].index.entry("bandit")
        self.assertEqual(entry.tier, PROJECTILE)
        self.assertIsNone(octopus_tier_for(entry.tier))

    def test_snapshot_provenance_survives_the_crossing(self):
        """A cross-cell target is still resolved against a snapshot, and the
        result still says which (Scope §7 / §15.10)."""
        from lobster.spatial import SNAPSHOT_CELL_ENTRY
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 45.0, 20.0)
        entry = self.manager.resident[FIELD].index.entry("bandit")
        self.assertEqual(hits[0].snapshot_reason, SNAPSHOT_CELL_ENTRY)
        self.assertEqual(hits[0].snapshot_seq, entry.snapshot_seq,
                         "the crossing must carry the target cell's own "
                         "provenance, not the shooter's")

    def test_a_refreshed_snapshot_is_visible_through_the_crossing(self):
        from lobster.spatial import SNAPSHOT_EXPLICIT
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        index = self.manager.resident[FIELD].index
        index.refresh_snapshot("bandit", (60.0, 0.0, 10.0))
        self.manager.resident[FIELD].skeletons["bandit"].set_root(
            Transform(position=(60.0, 0.0, 10.0)))
        hits = self.world_tester().resolve_projectile(
            self.view, SHOOTER, NORTH, 45.0, 20.0)
        self.assertEqual(hits[0].snapshot_reason, SNAPSHOT_EXPLICIT)
        self.assertGreater(hits[0].snapshot_seq, 0)

    def test_a_dormant_target_is_still_not_a_candidate(self):
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0), DORMANT)
        self.assertEqual(
            self.world_tester().resolve_projectile(
                self.view, SHOOTER, NORTH, 45.0, 20.0), [])


class TestTheRayIsWhatMoves(unittest.TestCase):
    """A direction is not a point, and transforming it as one bends the shot."""

    def test_a_direction_is_rotated_not_translated(self):
        placement = Transform(position=(0.0, 0.0, 128.0))
        heading = (0.0, 0.0, 1.0)
        self.assertEqual(placement.inverse_rotate(heading), heading,
                         "a pure translation must leave a direction alone")
        self.assertEqual(placement.inverse_apply((0.0, 0.0, 136.0)),
                         (0.0, 0.0, 8.0))

    def test_rotation_round_trips(self):
        import math
        half = math.radians(90) * 0.5
        placement = Transform(position=(5.0, 0.0, 7.0),
                              rotation=(0.0, math.sin(half), 0.0,
                                        math.cos(half)))
        for vector in ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.3, 0.5, -0.8)):
            back = placement.inverse_rotate(placement.rotate(vector))
            for got, want in zip(back, vector):
                self.assertAlmostEqual(got, want, places=9)

    def test_a_shot_with_no_direction_is_refused(self):
        session = build_session()
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            view = OctopusBridge(session).frame()
            manager.set_player_cell(view, VILLAGE)
            tester = WorldHitTester.from_manager(manager, view)
            with self.assertRaises(HitTestError):
                tester.resolve_projectile(view, SHOOTER, (0.0, 0.0, 0.0),
                                          45.0, 20.0)


class TestCost(CrossCellFixture):

    def test_a_cell_the_shot_cannot_reach_is_not_swept(self):
        """A shot must not pay a broad-phase sweep in every resident cell."""
        self.stand(VILLAGE, "guard", (60.0, 0.0, 118.0), ACTIVE)
        tester = self.world_tester()
        tester.begin_frame()
        # fire south, away from the field entirely
        tester.resolve_projectile(self.view, SHOOTER, (0.0, 0.0, -1.0), 30.0,
                                  20.0)
        field_queries = self.manager.resident[FIELD].index.stats.queries
        self.assertEqual(field_queries, 0,
                         "the field is behind the shooter and must not have "
                         "been queried at all")

    def test_stats_aggregate_across_cells(self):
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        tester = self.world_tester()
        tester.begin_frame()
        tester.resolve_projectile(self.view, SHOOTER, NORTH, 45.0, 20.0)
        stats = tester.stats()
        self.assertGreater(stats.projectiles, 0)
        self.assertGreater(stats.capsule_tests, 0)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The cross-cell volley (D55)
# ---------------------------------------------------------------------------

#: The equivalence tests below are about *answers*, so they run with the frame
#: budget off. They have to: eighteen separate cross-cell shots into this crowd
#: trip `hit_test_frame_us`, and the volley does not, because a volley charges
#: each cell once. That difference is real and is asserted on purpose further
#: down - it is D45's budget property one layer up.
NO_BUDGET = dict(frame_budget_us=0, max_broad_candidates_per_frame=0,
                 max_capsule_tests_per_frame=0, max_bucket_scans_per_frame=0)


class VolleyFixture(CrossCellFixture):
    """A crowd on both sides of the boundary, and a fan of arrows into it."""

    def world_tester(self, bus=None, **ceilings):
        return WorldHitTester.from_manager(self.manager, self.view, bus,
                                           **(ceilings or NO_BUDGET))

    def crowd(self, per_cell=14):
        import random
        rng = random.Random(5)
        for i in range(per_cell):
            self.stand(VILLAGE, "v%d" % i,
                       (52.0 + rng.uniform(-6, 6), 0.0,
                        104.0 + rng.uniform(0, 18)),
                       ACTIVE if i % 2 else PROJECTILE)
            self.stand(FIELD, "f%d" % i,
                       (52.0 + rng.uniform(-6, 6), 0.0,
                        rng.uniform(0, 24)),
                       ACTIVE if i % 2 else PROJECTILE)

    def volley(self, count=18, seed=11):
        import random
        rng = random.Random(seed)
        return [((SHOOTER[0] + rng.uniform(-8, 8), 1.0, SHOOTER[2]),
                 (rng.uniform(-0.35, 0.35), 0.0, 1.0), 60.0, 20.0, "archer")
                for _ in range(count)]

    @staticmethod
    def digest(per_shot):
        return [[(h.target_id, h.cell_id, h.region, round(h.force, 6),
                  h.region_precise, h.snapshot_seq,
                  round(h.distance_from_source, 6)) for h in shot]
                for shot in per_shot]


class TestTheVolleyAnswersWhatTheShotsAnswer(VolleyFixture):
    """The property everything else rests on, one layer up from D45: the
    cross-cell volley must equal N cross-cell shots."""

    def compare(self, **kwargs):
        self.crowd()
        shots = self.volley()

        one = self.world_tester()
        one.begin_frame()
        separate = [one.resolve_projectile(self.view, o, d, m, f,
                                           source_id=s, **kwargs)
                    for o, d, m, f, s in shots]

        many = self.world_tester()
        many.begin_frame()
        batched = many.resolve_volley(self.view, shots, **kwargs)

        self.assertEqual(self.digest(separate), self.digest(batched))
        return separate

    def test_identical_for_the_first_hit(self):
        landed = sum(len(shot) for shot in self.compare())
        self.assertGreater(landed, 0,
                           "no arrow landed - the comparison is between two "
                           "empty lists")

    def test_identical_when_every_hit_is_wanted(self):
        landed = self.compare(first_hit_only=False)
        self.assertGreater(sum(len(s) for s in landed), 0)
        self.assertTrue(any(len(s) > 1 for s in landed),
                        "no shot passed through two targets, so the multi-hit "
                        "merge is untested")

    def test_both_cells_are_actually_reached(self):
        """Otherwise this is a very slow test of the single-cell volley."""
        self.crowd()
        hits = self.world_tester().resolve_volley(
            self.view, self.volley(), first_hit_only=False)
        cells = {h.cell_id for shot in hits for h in shot}
        self.assertEqual(cells, {VILLAGE, FIELD},
                         "the fixture only reaches {0}".format(sorted(cells)))

    def test_it_fires_the_same_events_in_the_same_order(self):
        self.crowd()
        shots = self.volley()

        one_bus, many_bus = EventBus(), EventBus()
        one = self.world_tester(one_bus)
        one.begin_frame()
        for o, d, m, f, s in shots:
            one.resolve_projectile(self.view, o, d, m, f, source_id=s)

        many = self.world_tester(many_bus)
        many.begin_frame()
        many.resolve_volley(self.view, shots)

        self.assertEqual([e.to_dict() for e in one_bus.log],
                         [e.to_dict() for e in many_bus.log])
        self.assertTrue(one_bus.log, "no events fired at all")

    def test_the_result_is_one_list_per_shot_in_order(self):
        self.crowd()
        shots = self.volley(count=7)
        answers = self.world_tester().resolve_volley(self.view, shots)
        self.assertEqual(len(answers), len(shots))
        self.assertTrue(all(isinstance(a, list) for a in answers))


class TestTheMergeIsByWorldDistance(VolleyFixture):
    """Each cell answers in its own coordinates. Taking whichever cell was
    asked first would let a shot hit the far bandit through the near one."""

    def test_the_nearer_target_wins_across_the_boundary(self):
        near = self.stand(VILLAGE, "near", (60.0, 0.0, 110.0))
        far = self.stand(FIELD, "far", (60.0, 0.0, 8.0))
        self.assertLess(distance(SHOOTER, near), distance(SHOOTER, far),
                        "the fixture has them the wrong way round")
        shot = (SHOOTER, NORTH, 60.0, 20.0, "archer")
        answers = self.world_tester().resolve_volley(self.view, [shot])
        self.assertEqual([h.target_id for h in answers[0]], ["near"])

    def test_the_order_does_not_depend_on_which_cell_holds_the_target(self):
        self.stand(VILLAGE, "near", (60.0, 0.0, 110.0))
        self.stand(FIELD, "far", (60.0, 0.0, 8.0))
        shot = (SHOOTER, NORTH, 60.0, 20.0, "archer")
        answers = self.world_tester().resolve_volley(self.view, [shot],
                                                     first_hit_only=False)
        ranges = [h.distance_from_source for h in answers[0]]
        self.assertEqual(ranges, sorted(ranges), "hits are not nearest first")
        self.assertEqual([h.target_id for h in answers[0]], ["near", "far"])


class TestWhatItRefusesToChange(VolleyFixture):

    def test_an_empty_volley_costs_nothing(self):
        self.crowd()
        tester = self.world_tester()
        tester.begin_frame()
        self.assertEqual(tester.resolve_volley(self.view, []), [])
        self.assertEqual(tester.stats().projectiles, 0)

    def test_a_shot_with_no_direction_still_raises(self):
        self.crowd()
        shots = self.volley(count=3)
        shots[1] = (SHOOTER, (0.0, 0.0, 0.0), 60.0, 20.0, "archer")
        with self.assertRaises(HitTestError):
            self.world_tester().resolve_volley(self.view, shots)

    def test_a_shot_that_reaches_no_cell_answers_nothing(self):
        """Fired at the sky, away from every cell. It must come back as an
        empty list rather than be dropped from the result."""
        self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        away = ((60.0, 400.0, 100.0), (0.0, 1.0, 0.0), 10.0, 20.0, "archer")
        landing = (SHOOTER, NORTH, 45.0, 20.0, "archer")
        answers = self.world_tester().resolve_volley(
            self.view, [away, landing])
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0], [])
        self.assertEqual([h.target_id for h in answers[1]], ["bandit"],
                         "the other arrow must land, or this test is "
                         "comparing one empty list with another")

    def test_a_dormant_entity_is_still_not_a_candidate(self):
        self.stand(FIELD, "sleeper", (60.0, 0.0, 8.0), DORMANT)
        shot = (SHOOTER, NORTH, 60.0, 20.0, "archer")
        self.assertEqual(
            self.world_tester().resolve_volley(self.view, [shot]), [[]])

    def test_every_hit_still_names_its_cell(self):
        self.crowd()
        answers = self.world_tester().resolve_volley(
            self.view, self.volley(), first_hit_only=False)
        for shot in answers:
            for hit in shot:
                self.assertIn(hit.cell_id, (VILLAGE, FIELD))


class TestTheBudgetIsChargedOncePerCell(VolleyFixture):
    """A volley is one frame's work and one decision about whether that frame
    fits - the same rule the single-cell volley follows (D45), applied across
    the resident set."""

    def loaded(self, bus=None):
        return WorldHitTester.from_manager(self.manager, self.view, bus)

    def test_the_same_volley_fits_as_a_batch_and_does_not_as_separate_shots(self):
        from lobster.budgets import BudgetViolation
        self.crowd()
        shots = self.volley(count=18)

        separate = self.loaded()
        separate.begin_frame()
        with self.assertRaises(BudgetViolation) as ctx:
            for o, d, m, f, s in shots:
                separate.resolve_projectile(self.view, o, d, m, f, source_id=s)
        self.assertEqual(ctx.exception.metric, "hit_test_frame_us")

        batched = self.loaded()
        batched.begin_frame()
        batched.resolve_volley(self.view, shots)

    def test_a_volley_big_enough_still_trips_it(self):
        """Charged once per cell, not waived. If this stopped raising, the
        budget would have become a number nothing enforces."""
        from lobster.budgets import BudgetViolation
        # A bigger *volley*, not a bigger crowd: this fixture's cells declare
        # `max_active_skeletons` of 8, so forty bodies a cell is refused long
        # before the frame budget gets a say.
        self.crowd()
        tester = self.loaded()
        tester.begin_frame()
        with self.assertRaises(BudgetViolation):
            tester.resolve_volley(self.view, self.volley(count=60))


class TestTheBatchingIsPerCellNotPerShot(VolleyFixture):
    """The reason this exists: a cross-cell volley resolved shot by shot
    rebuilds every cell's entity table once per arrow."""

    def test_each_cell_is_asked_once_however_many_arrows(self):
        self.crowd()
        shots = self.volley(count=12)
        tester = self.world_tester()
        calls = {}
        for cell_id, sub in tester.testers.items():
            original = sub.resolve_volley
            def counted(view, batch, _cell=cell_id, _f=original, **kw):
                calls[_cell] = calls.get(_cell, 0) + 1
                return _f(view, batch, **kw)
            sub.resolve_volley = counted
        tester.begin_frame()
        tester.resolve_volley(self.view, shots)
        self.assertTrue(calls, "no cell was asked at all")
        self.assertEqual(set(calls.values()), {1},
                         "a cell was asked more than once: {0}".format(calls))

    def test_a_shot_is_not_sent_to_a_cell_it_cannot_reach(self):
        """The `_ray_reaches` gate, per shot rather than per call - so a volley
        fanned across an arc does not sweep every cell for every arrow."""
        self.crowd()
        away = ((60.0, 400.0, 100.0), (0.0, 1.0, 0.0), 10.0, 20.0, "archer")
        shots = [away] + self.volley(count=6)
        tester = self.world_tester()
        sizes = {}
        for cell_id, sub in tester.testers.items():
            original = sub.resolve_volley
            def sized(view, batch, _cell=cell_id, _f=original, **kw):
                sizes[_cell] = len(batch)
                return _f(view, batch, **kw)
            sub.resolve_volley = sized
        tester.begin_frame()
        tester.resolve_volley(self.view, shots)
        self.assertTrue(sizes)
        for cell_id, size in sizes.items():
            self.assertLess(size, len(shots),
                            "{0} was handed the shot fired at the sky".format(
                                cell_id))


class TestAHitBelongsToTheShotThatMadeIt(VolleyFixture):
    """The bookkeeping the batching costs. A cell is handed a *subset* of the
    volley, so its answers are indexed by position in that subset, not by
    position in the volley - and the two stop agreeing the moment one arrow
    fails the reach gate. Getting this wrong credits the kill to the wrong
    archer, silently, with every hit still correct.
    """

    def setUp(self):
        super().setUp()
        # Four arrows whose reach sets deliberately differ: the sky shot
        # reaches nothing, `c` reaches only the village, and it sits *after*
        # the gap so a positional slip shows up in both cells.
        self.stand(FIELD, "alpha", (50.0, 0.0, 8.0))
        self.stand(FIELD, "beta", (70.0, 0.0, 8.0))
        self.stand(VILLAGE, "gamma", (40.0, 0.0, 110.0))
        self.shots = [
            ((50.0, 1.0, 100.0), NORTH, 60.0, 20.0, "a"),
            ((60.0, 400.0, 100.0), (0.0, 1.0, 0.0), 10.0, 20.0, "sky"),
            ((70.0, 1.0, 100.0), NORTH, 60.0, 20.0, "b"),
            ((40.0, 1.0, 100.0), NORTH, 20.0, 20.0, "c"),
        ]

    def test_the_fixture_hands_the_cells_different_subsets(self):
        """Otherwise every slot list is `range(len(volley))` and the
        bookkeeping below is untested."""
        tester = self.world_tester()
        reach = {}
        for index, (origin, direction, max_distance, _f, _s) in enumerate(
                self.shots):
            heading = normalize(direction)
            reach[index] = [
                cell_id for cell_id in sorted(tester.cells)
                if tester._ray_reaches(cell_id, tester.placement_of(cell_id),
                                       origin, heading, max_distance)]
        self.assertEqual(reach[1], [], "the sky shot reaches a cell")
        self.assertEqual(reach[3], [VILLAGE])
        self.assertEqual(reach[0], [FIELD, VILLAGE])
        self.assertNotEqual(reach[0], reach[3],
                            "every shot reaches the same cells, so a "
                            "positional slip would be invisible")

    def test_each_hit_comes_back_on_its_own_shot(self):
        answers = self.world_tester().resolve_volley(
            self.view, self.shots, first_hit_only=False)
        self.assertEqual([[h.target_id for h in shot] for shot in answers],
                         [["alpha"], [], ["beta"], ["gamma"]])

    def test_it_still_matches_the_shots_resolved_separately(self):
        one = self.world_tester()
        separate = [one.resolve_projectile(self.view, o, d, m, f, source_id=s,
                                           first_hit_only=False)
                    for o, d, m, f, s in self.shots]
        many = self.world_tester().resolve_volley(self.view, self.shots,
                                                  first_hit_only=False)
        self.assertEqual(self.digest(separate), self.digest(many))


class TestADirectionIsADirectionNotADistance(VolleyFixture):
    """`max_distance` is the range; the direction only says where. Nothing
    downstream re-normalises - `HitTester.resolve_volley` multiplies the
    direction by `max_distance` as given - so a volley that forwards the
    caller's vector unchanged makes a long vector shoot further than its range
    and a short one fall short of it.
    """

    def setUp(self):
        super().setUp()
        self.bandit = self.stand(FIELD, "bandit", (60.0, 0.0, 8.0))
        self.range_m = distance(SHOOTER, self.bandit)
        self.assertAlmostEqual(self.range_m, 36.0, places=1,
                               msg="the fixture moved; the ranges below are "
                                   "chosen around it")

    def shoot(self, direction, max_distance):
        answers = self.world_tester().resolve_volley(
            self.view, [(SHOOTER, direction, max_distance, 20.0, "archer")])
        return [h.target_id for h in answers[0]]

    def test_a_short_vector_does_not_shorten_the_shot(self):
        """|d| = 0.2 with 45 m of range: in range, and the arrow must land."""
        self.assertEqual(self.shoot((0.0, 0.0, 0.2), 45.0), ["bandit"])

    def test_a_long_vector_does_not_lengthen_the_shot(self):
        """|d| = 5 with 10 m of range: out of range, and it must stay out."""
        self.assertEqual(self.shoot((0.0, 0.0, 5.0), 10.0), [])
        self.assertEqual(self.shoot(NORTH, 10.0), [],
                         "the unit shot is in range too, so the test above "
                         "proves nothing")

    def test_scaling_the_direction_changes_nothing_at_all(self):
        unit = self.world_tester().resolve_volley(
            self.view, [(SHOOTER, NORTH, 45.0, 20.0, "archer")],
            first_hit_only=False)
        for scale in (0.2, 5.0, 100.0):
            scaled = self.world_tester().resolve_volley(
                self.view,
                [(SHOOTER, (0.0, 0.0, scale), 45.0, 20.0, "archer")],
                first_hit_only=False)
            self.assertEqual(self.digest(unit), self.digest(scaled),
                             "|d| = {0} answered differently".format(scale))
