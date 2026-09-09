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
from lobster.cell import CellManager
from lobster.constants import (DEFAULT_MAX_CELL_BYTES, MAX_RESIDENT_CELLS,
                               MAX_TRANSITION_PEAK_BYTES)
from lobster.octopus_bridge import OctopusBridge
from tests.fixtures import (FIELD, GATEHOUSE, KEEP, VILLAGE, BundleWorkspace,
                            build_session, plain_bundle, standard_workspace,
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
        mib = 1024 * 1024
        package = {"format": "lce-package", "package_id": "mod.greedy",
                   "version": "1.0.0", "schema_compat": {"min": 1, "max": 1},
                   "operations": [
                       {"op": "PATCH", "id": cell, "field": "lobster_budget",
                        "value": {"max_bytes": 21 * mib}}
                       for cell in (VILLAGE, FIELD)]}
        session = build_session(extra_packages=[package])
        view = OctopusBridge(session).frame()

        findings = transition_peak_findings(view, peak_limit=30 * mib)
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
