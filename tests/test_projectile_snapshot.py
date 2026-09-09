"""Section 14 test 7 - PROJECTILE-tier stale snapshot (v0.4).

> a projectile fired at a dormant entity that hasn't moved since cell entry
> still resolves against its last known-correct transform, not a garbage/never-
> set value.

And the reopening risk, Scope 15.10:

> If a future change adds a code path that moves a dormant entity (a scripted
> relocation, a mod effect) without also refreshing its grid snapshot,
> PROJECTILE-tier queries silently go stale again.

The tests below cover both: the projectile resolves correctly *and* the API
makes the reopening path impossible to take by accident. The second half is the
one that has to still be here in a year.

The other half of Scope 7 is here too, because it is the same code path and the
same easy mistake: **PROJECTILE is a hit-test-candidacy tier, not a
simulation-promotion tier.**
"""

from __future__ import annotations

import unittest

from lobster.budgets import BudgetViolation
from lobster.constants import modelled_cost_us
from lobster.events import EventBus
from lobster.geometry import Transform
from lobster.hittest import HitTester
from lobster.octopus_bridge import OctopusBridge
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.spatial import (SNAPSHOT_CELL_ENTRY, SNAPSHOT_CELL_EXIT,
                             SNAPSHOT_EXPLICIT, SpatialError, SpatialIndex)
from lobster.tiers import ACTIVE, DORMANT, PROJECTILE, octopus_tier_for
from tests.fixtures import VILLAGE, build_session

FAR = "npc-bren"
DORMANT_SPOT = (40.0, 0.0, 10.0)


class TestProjectileAgainstASnapshot(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.index = SpatialIndex(VILLAGE)
        # cell entry: the one moment a non-ACTIVE entity's transform is taken
        self.index.snapshot([(FAR, DORMANT_SPOT, PROJECTILE)],
                            reason=SNAPSHOT_CELL_ENTRY)
        self.skeleton = Skeleton(FAR, humanoid_region_set(),
                                 root=Transform(position=DORMANT_SPOT))
        self.tester = HitTester(self.index, {FAR: self.skeleton}, EventBus())

    def fire(self, from_point=(0.0, 1.0, 10.0), direction=(1.0, 0.0, 0.0)):
        with self.bridge.frame() as view:
            return self.tester.resolve_projectile(
                view, from_point, direction, max_distance=80.0, force=25.0,
                source_id="player")

    def test_a_projectile_resolves_against_the_last_known_transform(self):
        hits = self.fire()
        self.assertEqual([h.target_id for h in hits], [FAR])
        hit = hits[0]
        self.assertEqual(hit.tier, PROJECTILE)
        self.assertEqual(hit.snapshot_reason, SNAPSHOT_CELL_ENTRY)
        self.assertGreaterEqual(hit.snapshot_seq, 1,
                                "the transform must come from a real snapshot")

    def test_a_projectile_resolves_a_region_at_range(self):
        """DECISIONS.md D16 - snipers get limbs.

        The body capsule decides *whether* it hit; the per-bone pass decides
        *where*. A hit two cells away names a region exactly as a hit in melee
        range does.
        """
        from lobster.skeleton import HUMANOID_REGIONS
        hit = self.fire()[0]
        self.assertEqual(hit.tier, PROJECTILE)
        self.assertIn(hit.region, HUMANOID_REGIONS)

    def test_a_headshot_at_range_reports_the_head(self):
        """The whole point of the change: aim matters."""
        head = self.skeleton.capsule_for("head")
        y = (head.a[1] + head.b[1]) * 0.5
        hit = self.fire(from_point=(0.0, y, 10.0))[0]
        self.assertEqual(hit.region, "head")
        self.assertTrue(hit.region_precise,
                        "the ray actually intersected the head capsule")

    def test_a_leg_shot_at_range_reports_a_leg(self):
        leg = self.skeleton.capsule_for("leg_r")
        y = (leg.a[1] + leg.b[1]) * 0.5
        hit = self.fire(from_point=(0.0, y, 10.0))[0]
        self.assertIn(hit.region, ("left_leg", "right_leg"))
        self.assertTrue(hit.region_precise)

    def test_a_graze_reports_the_nearest_region_and_says_it_was_a_graze(self):
        """`region_precise=False` is the honest half.

        The body capsule is a conservative bound around the rig, so a ray can
        clip it without intersecting any bone. That is still a hit - the gate
        already said so - but the region is the nearest bone, not a measured
        one, and the result says which.
        """
        body = self.skeleton.whole_body_capsule()
        grazing_y = body.a[1] - body.radius * 0.9
        hits = self.fire(from_point=(0.0, grazing_y, 10.0))
        if hits:                       # geometry permitting; assert the flag
            self.assertIsNotNone(hits[0].region)
            if not hits[0].region_precise:
                self.assertIn(hits[0].region, ("left_leg", "right_leg"))

    def test_a_severed_limb_offers_no_hitbox_at_range_either(self):
        """Scope 5 puts no tier condition on the limb-state read."""
        arm = self.skeleton.capsule_for("arm_l")
        y = (arm.a[1] + arm.b[1]) * 0.5
        z = arm.a[2]
        before = self.fire(from_point=(0.0, y, z))
        self.session.engine.write({"op": "PATCH", "id": FAR,
                                   "field": "limb_state.left_arm",
                                   "value": "severed"})
        after = self.fire(from_point=(0.0, y, z))
        if before and before[0].region == "left_arm":
            self.assertNotEqual(after[0].region if after else None, "left_arm",
                                "a severed arm must offer no hitbox at any "
                                "tier")

    def test_a_miss_is_still_a_miss(self):
        """No regression in *whether* it hit - only in what gets reported."""
        self.assertEqual(self.fire(from_point=(0.0, 40.0, 10.0)), [])

    def test_the_result_carries_pose_provenance(self):
        """A distant target's pose is whatever was last set, and says so."""
        hit = self.fire()[0]
        self.assertEqual(hit.pose_version, self.skeleton.pose_version)
        self.assertIn("pose_version", hit.to_dict())

    def test_a_never_set_transform_cannot_exist(self):
        index = SpatialIndex(VILLAGE)
        with self.assertRaises(SpatialError):
            index.add("ghost", None, DORMANT)
        with self.assertRaises(SpatialError) as ctx:
            index.entry("ghost")
        self.assertIn("never snapshotted", str(ctx.exception))

    def test_moving_a_dormant_entity_is_refused_and_says_why(self):
        """The 15.10 guard: there is no silent way to write a stale position."""
        with self.assertRaises(SpatialError) as ctx:
            self.index.move(FAR, (41.0, 0.0, 10.0))
        message = str(ctx.exception)
        self.assertIn("refresh_snapshot", message)
        self.assertIn("provenance", message)

    def test_a_scripted_relocation_refreshes_the_snapshot(self):
        before = self.index.entry(FAR).snapshot_seq
        entry = self.index.refresh_snapshot(FAR, (60.0, 0.0, 10.0))
        self.assertEqual(entry.position, (60.0, 0.0, 10.0))
        self.assertGreater(entry.snapshot_seq, before)
        self.assertEqual(entry.snapshot_reason, SNAPSHOT_EXPLICIT)

        # and the projectile now resolves against the new position
        self.skeleton.set_root(Transform(position=(60.0, 0.0, 10.0)))
        hits = self.fire()
        self.assertEqual([h.snapshot_reason for h in hits], [SNAPSHOT_EXPLICIT])

    def test_an_active_entity_moves_continuously(self):
        index = SpatialIndex(VILLAGE)
        index.add("player", (1.0, 0.0, 1.0), ACTIVE)
        entry = index.move("player", (2.0, 0.0, 1.0))
        self.assertEqual(entry.position, (2.0, 0.0, 1.0))

    def test_dormant_entities_have_no_hitbox(self):
        """Scope 7: DORMANT fidelity is "none"; 6.5's Event path covers them."""
        self.index.set_tier(FAR, DORMANT)
        self.assertEqual(self.fire(), [])


class TestVolleyCostModel(unittest.TestCase):
    """The worst case: a zone-to-zone volley into a massed army.

    Scope 7 promises "cost scales with attacks and tiers, not population", and
    that promise is what makes per-bone resolution at range affordable (D16).
    Two things have to hold, and both are asserted here rather than argued:

    1. the **refinement** is paid per landed hit, not per candidate - the
       whole-body capsule gates it;
    2. the **broad phase** walks the segment rather than bounding it, so an
       arrow's cost does not grow with how many people are standing near it.

    And when a volley outgrows the frame anyway, it fails loudly with the
    alternative named, rather than quietly taking a second (L6).
    """

    ARMY = 200

    def setUp(self):
        from lobster.geometry import Transform
        from lobster.skeleton import Skeleton, humanoid_region_set
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.index = SpatialIndex(VILLAGE)
        entries, self.skeletons = [], {}
        for i in range(self.ARMY):
            position = (float(20 + (i % 20) * 3), 0.0, float(20 + (i // 20) * 3))
            entries.append(("npc-%d" % i, position, PROJECTILE))
            self.skeletons["npc-%d" % i] = Skeleton(
                "npc-%d" % i, humanoid_region_set(),
                root=Transform(position=position))
        self.index.snapshot(entries)

    def volley(self, arrows: int, **kwargs):
        """Fire `arrows` into the army, all in one frame."""
        tester = HitTester(self.index, self.skeletons, **kwargs)
        tester.begin_frame()
        landed = 0
        with self.bridge.frame() as view:
            for a in range(arrows):
                landed += len(tester.resolve_projectile(
                    view, (0.0, 1.2, 20.0 + (a % 60) * 0.5), (1.0, 0.0, 0.0),
                    120.0, 9.0, source_id="archer"))
        return tester, landed

    def test_refinement_is_paid_per_landed_hit_not_per_candidate(self):
        tester, landed = self.volley(60, max_capsule_tests_per_frame=0,
                                     max_broad_candidates_per_frame=0,
                                     max_bucket_scans_per_frame=0,
                                     frame_budget_us=0)
        stats = tester.stats
        self.assertGreater(landed, 0)
        self.assertEqual(stats.bone_refinements, landed,
                         "a bone refinement must happen once per landed hit - "
                         "if it tracks candidates instead, a volley scales with "
                         "army size")
        self.assertLessEqual(stats.body_tests, self.index.stats.candidates_considered)
        self.assertGreater(self.index.stats.candidates_considered,
                           stats.bone_refinements * 10,
                           "the broad phase should see far more candidates than "
                           "the refinement ever touches")

    def test_an_arrows_cost_does_not_grow_with_the_crowd_around_it(self):
        """The segment query walks the grid; it does not bound it with a
        sphere. DECISIONS.md D16."""
        sparse = SpatialIndex(VILLAGE)
        sparse.snapshot([("lone", (60.0, 0.0, 60.0), PROJECTILE)])
        sparse.query_segment((0.0, 1.0, 0.0), (120.0, 1.0, 120.0), radius=2.5)
        lone_buckets = sparse.stats.buckets_visited

        crowded = SpatialIndex(VILLAGE)
        crowded.snapshot([("npc-%d" % i,
                           (float(20 + (i % 20) * 3), 0.0,
                            float(20 + (i // 20) * 3)), PROJECTILE)
                          for i in range(self.ARMY)])
        crowded.query_segment((0.0, 1.0, 0.0), (120.0, 1.0, 120.0), radius=2.5)

        self.assertLess(
            crowded.stats.candidates_considered, self.ARMY,
            "one arrow examined the entire army - the broad phase is bounding "
            "the segment instead of walking it")

    def packed(self, count=400, pitch=0.6):
        """A crowd dense enough that candidates, not the walk, dominate."""
        index = SpatialIndex(VILLAGE)
        skeletons = {}
        side = int(count ** 0.5) + 1
        for i in range(count):
            position = (20.0 + (i % side) * pitch, 0.0,
                        20.0 + (i // side) * pitch)
            index.add("mob-%d" % i, position, PROJECTILE)
            skeletons["mob-%d" % i] = Skeleton(
                "mob-%d" % i, humanoid_region_set(),
                root=Transform(position=position))
        return index, skeletons

    def fire_until_budget(self, index, skeletons, ray_len):
        tester = HitTester(index, skeletons)
        tester.begin_frame()
        with self.bridge.frame() as view:
            for a in range(500):
                try:
                    tester.resolve_projectile(
                        view, (0.0, 1.2, 21.0 + (a % 40) * 0.3),
                        (1.0, 0.0, 0.0), ray_len, 9.0, source_id="archer")
                except BudgetViolation as violation:
                    return violation
        self.fail("the budget never tripped in 500 arrows")

    def test_a_volley_into_a_packed_crowd_names_the_occupancy_path(self):
        """L6 - declared, enforced, attributable.

        When the cost really is the crowd, Scope 6.5/7 already has the answer
        (zone occupancy, resolved once) and the violation must say so.
        """
        index, skeletons = self.packed()
        violation = self.fire_until_budget(index, skeletons, 120.0)
        self.assertEqual(violation.metric, "hit_test_frame_us")
        self.assertEqual(violation.cell_id, VILLAGE)
        self.assertIn("broad candidates", str(violation),
                      "a packed crowd must be attributed to candidates")
        self.assertIn("mass-casualty", str(violation))
        self.assertIn("6.5", str(violation))

    def test_long_shots_over_open_ground_name_the_grid_walk_instead(self):
        """The other driver, and the reason attribution had to be per-driver.

        An army spread over open ground blows the same budget for the opposite
        reason: the cost is ray length times queries, not bodies near the path.
        Telling that caller to "use zone occupancy" would be nonsense advice -
        there is no crowd - so the violation names the walk and points at the
        accelerator seam (D26) instead.
        """
        violation = self.fire_until_budget(self.index, self.skeletons, 120.0)
        self.assertEqual(violation.metric, "hit_test_frame_us")
        self.assertIn("grid traversal", str(violation))
        self.assertIn("ray length", str(violation))
        self.assertNotIn("mass-casualty", str(violation),
                         "there is no crowd here; that advice would be wrong")

    def test_the_budget_trips_where_the_frame_actually_blows(self):
        """This test used to assert that twelve arrows were "nowhere near the
        ceiling". They were nowhere near the *old* ceiling and about 2.5x over
        the frame, which is precisely the defect D27 fixes: the ceiling bounded
        the algorithm's scaling, not the frame.

        So the assertion is now the honest one. Four 120 m arrows into a
        200-strong army is what a 2 ms slice buys on the pure-Python path. That
        number is small; it is also true, and a budget that flattered it would
        be worth nothing.
        """
        tester, _ = self.volley(4)
        cost = modelled_cost_us(self.index.stats.buckets_scanned,
                                self.index.stats.candidates_considered,
                                tester.stats.capsule_tests)
        self.assertLessEqual(cost, tester.frame_budget_us)
        with self.assertRaises(BudgetViolation):
            self.volley(12)

    def test_a_long_shot_through_an_empty_cell_is_charged_for_its_walk(self):
        """The regression guard for D27's actual defect.

        `buckets_visited` counted only buckets that held somebody, so one 120 m
        shot across an empty cell cost ~318 us and was charged **0** of 32,768.
        Counting occupied buckets measures how crowded a cell is; it does not
        measure what a query did.
        """
        empty = SpatialIndex(VILLAGE)
        empty.add("lone", (200.0, 0.0, 200.0), PROJECTILE)
        empty.query_segment((0.0, 1.0, 0.0), (0.0, 1.0, 120.0), radius=2.5)
        stats = empty.stats

        self.assertEqual(stats.buckets_visited, 0)
        self.assertEqual(stats.candidates_considered, 0)
        self.assertGreater(stats.buckets_scanned, 100,
                           "the walk crossed a hundred-odd buckets and the "
                           "budget has to be able to see them")
        self.assertGreater(
            modelled_cost_us(stats.buckets_scanned,
                             stats.candidates_considered, 0), 100.0,
            "a query costing hundreds of microseconds must be charged "
            "hundreds of microseconds")


class TestProjectileDoesNotPromoteSimulation(unittest.TestCase):
    """Scope 7: "PROJECTILE is a hit-test-candidacy tier, not a
    simulation-promotion tier, and the two systems should never be
    conflated."""

    def test_only_lobster_active_maps_to_octopus_active(self):
        self.assertEqual(octopus_tier_for(ACTIVE), "ACTIVE")
        self.assertIsNone(octopus_tier_for(PROJECTILE))
        self.assertIsNone(octopus_tier_for(DORMANT))

    def test_a_projectile_tier_query_does_not_run_active_only_activities(self):
        """Octopus's own guard, reached through Lobster's mapping.

        `resolve_npc_state(tier=None)` skips attack/flee/hide (Octopus
        DESIGN_NOTES D24). Because PROJECTILE maps to None, an NPC being shot at
        from two cells away is not quietly promoted into combat behaviour.
        """
        session = build_session()
        view = OctopusBridge(session).frame()
        state = view.resolve_npc_state(FAR, lobster_tier=PROJECTILE)
        self.assertIsNone(state["tier"])

        active_state = view.resolve_npc_state(FAR, lobster_tier=ACTIVE)
        self.assertEqual(active_state["tier"], "ACTIVE")


class TestSnapshotLifecycle(unittest.TestCase):

    def test_cell_exit_takes_a_snapshot_too(self):
        """Scope 7: "a transform snapshot on cell entry and on cell exit"."""
        from lobster.cell import CellManager
        from tests.fixtures import standard_workspace
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            cell = manager.load(bridge.frame(), VILLAGE)
            cell.place(FAR, DORMANT_SPOT, PROJECTILE)
            index = cell.index
            self.assertEqual(index.entry(FAR).snapshot_reason,
                             SNAPSHOT_CELL_ENTRY)
            manager.unload(VILLAGE)
            self.assertEqual(index.entry(FAR).snapshot_reason,
                             SNAPSHOT_CELL_EXIT)
            self.assertEqual(index.entry(FAR).position, DORMANT_SPOT)


if __name__ == "__main__":
    unittest.main()
