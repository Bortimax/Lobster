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
