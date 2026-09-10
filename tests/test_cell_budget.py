"""Section 14 test 4 - cell-transition peak memory, asserted in CI.

> Cell-transition peak-memory budget, asserted in CI, not eyeballed.

"Not eyeballed" is why the ledger is *accounted* rather than sampled: every
artifact declares its own byte cost and the loader charges exactly that, so the
number this test asserts on is deterministic and does not move with the
allocator, the Python version or the machine.

Scope 15.5: "Cell transition is a budget moment, not a loading screen." The
transition loads before it unloads, so for a few frames both residency sets are
charged - and that union, not either side, is what has to fit.
"""

from __future__ import annotations

import unittest

from lobster.budgets import (Budget, BudgetViolation, MemoryLedger,
                             POOL_STRUCTURES, POOL_TERRAIN)
from lobster.cell import CellManager, residency_ring
from lobster.constants import (DEFAULT_MAX_CELL_BYTES, MAX_RESIDENT_CELLS,
                               MAX_TRANSITION_PEAK_BYTES)
from lobster.events import EventBus
from lobster.octopus_bridge import OctopusBridge
from tests.fixtures import (FAR_CELL, FIELD, GATEHOUSE, HOME_CELL, KEEP,
                            VILLAGE, BundleWorkspace, build_session,
                            disjoint_world, plain_bundle, standard_workspace,
                            village_bundle)


class TestCellTransitionBudget(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)

    def test_transition_peak_is_within_the_declared_ceiling(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            manager.set_player_cell(self.bridge.frame(), KEEP)

            report = manager.ledger.report()
            self.assertLessEqual(report["peak_bytes"], MAX_TRANSITION_PEAK_BYTES,
                                 report)
            self.assertLessEqual(report["peak_resident_cells"],
                                 MAX_RESIDENT_CELLS, report)
            self.assertEqual(report["resident_cells"], [KEEP])

    def test_the_peak_really_is_the_union_of_both_residency_sets(self):
        """Proof that the assertion above is measuring the transition moment.

        If the manager ever unloaded first, the peak would be one cell's worth
        and this budget would be measuring nothing.
        """
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            before = manager.ledger.total_bytes()
            manager.set_player_cell(self.bridge.frame(), KEEP)

            during = max(entry["resident_cells"] for entry in
                         manager.ledger.history)
            self.assertGreaterEqual(during, 3,
                                    "village + field + keep must be resident at "
                                    "the same moment during the transition")
            self.assertGreater(manager.ledger.peak_bytes, before,
                               "the peak must exceed either side alone")

    def test_over_budget_content_names_the_cell_and_the_record(self):
        """Scope 13 invariant 2: failure is visible and attributable."""
        tiny = {"lobster_budget": {"max_structure_voxels": 100}}
        with self.assertRaises(BudgetViolation) as ctx:
            Budget.declared(VILLAGE, tiny).check(
                "max_structure_voxels", 4096, record_id=GATEHOUSE)
        message = str(ctx.exception)
        self.assertIn(VILLAGE, message)
        self.assertIn(GATEHOUSE, message)
        self.assertIn("max_structure_voxels", message)
        self.assertEqual(ctx.exception.to_dict()["record_id"], GATEHOUSE)

    def test_a_cell_may_not_declare_a_higher_ceiling_than_the_shell(self):
        raised = {"lobster_budget": {"max_bytes": DEFAULT_MAX_CELL_BYTES * 2}}
        with self.assertRaises(BudgetViolation) as ctx:
            Budget.declared(VILLAGE, raised)
        self.assertIn("never a higher one", str(ctx.exception))

    def test_a_declared_cell_budget_is_enforced_at_load(self):
        """A cell whose authored content does not fit says so, by name."""
        package = {"format": "lce-package", "package_id": "mod.tiny_budget",
                   "version": "1.0.0", "schema_compat": {"min": 1, "max": 1},
                   "operations": [{"op": "PATCH", "id": VILLAGE,
                                   "field": "lobster_budget",
                                   "value": {"max_micro_chunks": 2}}]}
        session = build_session(extra_packages=[package])
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            with self.assertRaises(BudgetViolation) as ctx:
                manager.load(bridge.frame(), VILLAGE)
        self.assertEqual(ctx.exception.metric, "max_micro_chunks")
        self.assertEqual(ctx.exception.cell_id, VILLAGE)

    def test_the_peak_ceiling_itself_fires(self):
        """The assertion has teeth: a ledger with a tiny ceiling raises."""
        with standard_workspace() as ws:
            manager = CellManager(ws.path, ledger=MemoryLedger(peak_limit=1024))
            with self.assertRaises(BudgetViolation) as ctx:
                manager.set_player_cell(self.bridge.frame(), VILLAGE)
        self.assertEqual(ctx.exception.metric, "peak_transition_bytes")
        self.assertIn("cells resident", str(ctx.exception))

    def test_resident_cell_count_is_capped(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path,
                                  ledger=MemoryLedger(max_resident_cells=1))
            with self.assertRaises(BudgetViolation) as ctx:
                manager.set_player_cell(self.bridge.frame(), VILLAGE)
        self.assertEqual(ctx.exception.metric, "max_resident_cells")

    def test_an_interior_does_not_preload_its_neighbours(self):
        """DECISIONS.md D8 - a hub with twelve doors is not twelve cells."""
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), KEEP)
            self.assertEqual(sorted(manager.resident), [KEEP])

    def test_an_exterior_preloads_its_exterior_ring_only(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            # the village connects to the field (exterior) and the keep
            # (interior); only the field joins residency
            self.assertEqual(sorted(manager.resident), [FIELD, VILLAGE])

    def test_the_active_skeleton_ceiling_is_enforced(self):
        """Scope 14: "Max ACTIVE-tier (full-skeleton) NPCs simultaneously
        resident - worth a hard number next." The fixture village declares 8."""
        from lobster.tiers import ACTIVE, DORMANT
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            cell = manager.load(self.bridge.frame(), VILLAGE)
            self.assertEqual(cell.budget.max_active_skeletons, 8)
            for i in range(8):
                cell.place("npc-{0}".format(i), (float(i), 0.0, 0.0), ACTIVE)
            with self.assertRaises(BudgetViolation) as ctx:
                cell.place("npc-9", (9.0, 0.0, 0.0), ACTIVE)
            self.assertEqual(ctx.exception.metric, "max_active_skeletons")
            self.assertEqual(ctx.exception.record_id, "npc-9")
            # a DORMANT entity costs nothing against that ceiling
            cell.place("npc-9", (9.0, 0.0, 0.0), DORMANT)
            self.assertEqual(cell.index.active_count(), 8)

    def test_the_defaults_compose_with_the_residency_ceiling(self):
        """Shrimp finding #3 - three numbers that contradicted each other.

        9 resident cells x a 48 MiB default was 432 MiB against a 192 MiB peak,
        so a cell that declared nothing got a ceiling it could never be allowed
        to use. The default is derived from the peak now, so this holds by
        construction rather than by anyone remembering (DECISIONS.md D20).
        """
        from lobster.constants import DEFAULT_MAX_CELL_BYTES
        self.assertLessEqual(
            MAX_RESIDENT_CELLS * DEFAULT_MAX_CELL_BYTES,
            MAX_TRANSITION_PEAK_BYTES,
            "a full residency of cells that each declare nothing must fit "
            "inside the transition peak")

    def test_a_world_of_defaults_reports_no_composition_finding(self):
        from lobster.budgets import transition_peak_findings
        view = self.bridge.frame()
        self.assertEqual(transition_peak_findings(view), [])

    def test_budgets_that_do_not_compose_are_reported_by_name(self):
        """The check Shrimp asked for: declared ceilings summed over the union
        a transition actually holds."""
        from lobster.budgets import transition_peak_findings
        # Expressed against the default rather than in megabytes: a cell may
        # declare *lower* and never higher, so a literal that happened to be
        # under the default became a `BudgetViolation` the moment the default
        # moved - which it did when the model library took a share of the
        # transition peak (D51). Two cells at the ceiling against a peak that
        # only fits one and a bit is the scenario, whatever the ceiling is.
        greedy = DEFAULT_MAX_CELL_BYTES
        package = {"format": "lce-package", "package_id": "mod.greedy",
                   "version": "1.0.0", "schema_compat": {"min": 1, "max": 1},
                   "operations": [
                       {"op": "PATCH", "id": cell, "field": "lobster_budget",
                        "value": {"max_bytes": greedy}}
                       for cell in (VILLAGE, FIELD)]}
        session = build_session(extra_packages=[package])
        view = OctopusBridge(session).frame()

        findings = transition_peak_findings(view, peak_limit=int(greedy * 1.5))
        by_cell = {f["record_id"]: f for f in findings}
        self.assertIn(VILLAGE, by_cell)
        finding = by_cell[VILLAGE]
        self.assertEqual(finding["code"], "over_transition_peak")
        self.assertGreater(finding["declared_bytes"], finding["limit"])
        self.assertIn(FIELD, finding["resident"],
                      "the finding must name the ring that pushes it over, not "
                      "just the cell")
        self.assertIn("leave room for its ring", finding["detail"])

    def test_the_check_uses_the_same_residency_rule_as_the_loader(self):
        """One definition of the ring, or the prediction is about a different
        world than the one that loads."""
        from lobster.cell import residency_ring
        view = self.bridge.frame()
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            self.assertEqual(sorted(manager.desired_residency(view, VILLAGE)),
                             sorted(residency_ring(view, VILLAGE)))
            self.assertEqual(sorted(manager.desired_residency(view, KEEP)),
                             sorted(residency_ring(view, KEEP)))

    def test_unload_releases_the_charge(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            charged = manager.ledger.total_bytes()
            self.assertGreater(charged, 0)
            manager.unload(FIELD)
            self.assertLess(manager.ledger.total_bytes(), charged)
            self.assertNotIn(FIELD, manager.ledger.report()["resident_cells"])


if __name__ == "__main__":
    unittest.main()


class TestFastTravelBetweenDisjointRings(unittest.TestCase):
    """Shrimp finding #6 (DECISIONS.md D32).

    `set_player_cell` held the union of both residency sets across every move.
    For a walk that is correct and budgeted. For a jump between two exterior
    cells whose rings share nothing it is `2 x ring` - 10 against a ceiling of
    9 - and the failure surfaced as a `BudgetViolation` naming a third cell
    with nothing to do with either endpoint.
    """

    def setUp(self):
        self.session, self.ws, self.ids = disjoint_world()
        self.addCleanup(self.ws.close)
        self.bridge = OctopusBridge(self.session)
        self.manager = CellManager(self.ws.path)

    def view(self):
        return self.bridge.frame()

    def test_the_two_rings_really_are_disjoint(self):
        """Otherwise the rest of this class proves nothing."""
        view = self.view()
        home = set(residency_ring(view, HOME_CELL, ring=1))
        far = set(residency_ring(view, FAR_CELL, ring=1))
        self.assertEqual(len(home), 5)
        self.assertEqual(len(far), 5)
        self.assertEqual(home & far, set())
        self.assertGreater(len(home | far), MAX_RESIDENT_CELLS,
                           "the union must exceed the ceiling or there is "
                           "nothing here to fix")

    def test_fast_travel_stays_within_the_ceiling(self):
        self.manager.set_player_cell(self.view(), HOME_CELL)
        self.manager.set_player_cell(self.view(), FAR_CELL)

        self.assertEqual(len(self.manager.resident), 5)
        self.assertEqual(self.manager.player_cell, FAR_CELL)
        peak = max(e["resident_cells"] for e in self.manager.ledger.history)
        self.assertLessEqual(peak, MAX_RESIDENT_CELLS, "the jump held both rings")

    def test_a_walk_still_holds_the_union(self):
        """The fix must not buy fast travel by deleting the transition peak
        that `MAX_TRANSITION_PEAK_BYTES` exists to bound."""
        self.manager.set_player_cell(self.view(), HOME_CELL)
        before = max(e["resident_cells"] for e in self.manager.ledger.history)
        self.manager.set_player_cell(self.view(), "cell-home-2-1")
        during = max(e["resident_cells"] for e in self.manager.ledger.history)
        self.assertGreater(during, before,
                           "stepping to a neighbour must still peak above "
                           "either ring alone")

    def test_a_jump_reports_exit_before_enter(self):
        """§13 fixes the Events, not their order. Across a teleport the truer
        sequence is leaving and then arriving, and that is now what happens."""
        bus = EventBus()
        manager = CellManager(self.ws.path, bus=bus)
        manager.set_player_cell(self.view(), HOME_CELL)
        del bus.log[:]
        manager.set_player_cell(self.view(), FAR_CELL)

        names = [e.name for e in bus.log]
        self.assertIn("on_exit_cell", names)
        self.assertIn("on_enter_cell", names)
        self.assertLess(names.index("on_exit_cell"), names.index("on_enter_cell"))


class TestWhatCountsAsAJump(unittest.TestCase):
    """`is_continuous_move` is the whole rule, so it gets its own truth table.

    The first version of this fix tested only "is the destination resident",
    which classified every walk through a keep door as a teleport: an interior
    is never in an exterior's ring (D8). The connection check is what makes a
    door continuous.
    """

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)

    def test_a_cell_already_resident_is_a_walk(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            self.assertIn(FIELD, manager.resident)
            self.assertTrue(
                manager.is_continuous_move(self.bridge.frame(), FIELD))

    def test_an_unloaded_interior_behind_a_door_is_still_a_walk(self):
        """The case the first draft got wrong."""
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            self.assertNotIn(KEEP, manager.resident,
                             "an interior is not preloaded (D8) - that is why "
                             "residency alone is the wrong test")
            self.assertTrue(
                manager.is_continuous_move(self.bridge.frame(), KEEP),
                "walking through a door is continuous even though the room "
                "behind it was not resident")

    def test_an_unconnected_unloaded_cell_is_a_jump(self):
        session, ws, _ids = disjoint_world()
        self.addCleanup(ws.close)
        bridge = OctopusBridge(session)
        manager = CellManager(ws.path)
        manager.set_player_cell(bridge.frame(), HOME_CELL)
        self.assertFalse(manager.is_continuous_move(bridge.frame(), FAR_CELL))

    def test_the_first_move_of_a_session_is_a_jump(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            self.assertIsNone(manager.player_cell)
            self.assertFalse(
                manager.is_continuous_move(self.bridge.frame(), VILLAGE),
                "nothing is resident, so there is nothing to preserve")

    def test_an_explicit_origin_overrides_where_the_player_is(self):
        """`from_location_id` is what the caller says it came through, and it
        is more authoritative than the manager's own last position."""
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            manager.set_player_cell(self.bridge.frame(), VILLAGE)
            self.assertTrue(manager.is_continuous_move(
                self.bridge.frame(), KEEP, from_location_id=VILLAGE))
            self.assertFalse(manager.is_continuous_move(
                self.bridge.frame(), KEEP, from_location_id=FIELD),
                "no authored connection runs from the field into the keep")
