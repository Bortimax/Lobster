"""The model budget (ASSET_SCOPE §7 step 6), derived rather than chosen.

§6 was explicit about the order and about what would have gone wrong:

> **No placeholder ceiling. Not even a temporary one.** ... A number invented
> before there is anything to measure survives: it acquires tests, it gets
> quoted in a document, and by the time real numbers exist it is load-bearing
> and nobody remembers it was a guess.

So the numbers here arrive last, and every one of them is a division. What these
tests pin is the *arithmetic* — that each ceiling still follows from a declared
slice and a measured unit, and that the whole set still composes — because a
derived number that someone later edits in place is indistinguishable from a
chosen one.
"""

from __future__ import annotations

import unittest

from lobster.budgets import Budget, BudgetViolation
from lobster.bundle import PropPlacement
from lobster.camera import Camera
from lobster.cell import ResidentCell
from lobster.constants import (DEFAULT_MAX_CELL_BYTES, MAX_ITEMS_PER_CELL,
                               MAX_MODEL_LIBRARY_BYTES,
                               MAX_PROP_PLACEMENTS_PER_CELL,
                               MAX_RESIDENT_CELLS,
                               MAX_TRANSITION_PEAK_BYTES,
                               MAX_VISIBLE_PLACEMENTS_PER_FRAME,
                               MODEL_DRAW_BUDGET_US, PER_PLACEMENT_US,
                               RESIDENT_MEMORY_SHARES)
from lobster.geometry import Transform
from lobster.model_library import ModelLibrary, ModelMesh
from lobster.visibility import (PROP, build_draw_list,
                                modelled_placement_cost_us)
from tests.fixtures import plain_bundle, primitive_library

CELL = "cell-b"


def props(count, model_ref="model-barrel"):
    return [PropPlacement(prop_id="p%d" % i, model_ref=model_ref,
                          transform=Transform(
                              position=(2.0 + (i % 40) * 0.25, 0.0,
                                        6.0 + (i // 40) * 0.25)))
            for i in range(count)]


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

class TestEveryCeilingIsStillADivision(unittest.TestCase):
    """Not one of these may become a number somebody typed."""

    def test_the_frame_ceiling_is_the_slice_over_the_measured_unit(self):
        self.assertEqual(MAX_VISIBLE_PLACEMENTS_PER_FRAME,
                         int(MODEL_DRAW_BUDGET_US / PER_PLACEMENT_US))

    def test_the_per_cell_ceiling_is_the_frame_over_the_ring(self):
        self.assertEqual(MAX_PROP_PLACEMENTS_PER_CELL,
                         int(MAX_VISIBLE_PLACEMENTS_PER_FRAME
                             / MAX_RESIDENT_CELLS))

    def test_the_library_gets_one_share_of_the_peak(self):
        self.assertEqual(MAX_MODEL_LIBRARY_BYTES,
                         MAX_TRANSITION_PEAK_BYTES // RESIDENT_MEMORY_SHARES)
        self.assertEqual(DEFAULT_MAX_CELL_BYTES, MAX_MODEL_LIBRARY_BYTES,
                         "the library is charged on the same terms as a cell, "
                         "so the two shares are the same size")

    def test_the_memory_numbers_compose(self):
        """D20's rule, with a tenth claimant: a default that cannot compose is
        a lie."""
        self.assertLessEqual(
            DEFAULT_MAX_CELL_BYTES * MAX_RESIDENT_CELLS
            + MAX_MODEL_LIBRARY_BYTES,
            MAX_TRANSITION_PEAK_BYTES)

    def test_the_frame_slices_compose(self):
        """Hit-testing, picking and drawing together stay under half a 60 FPS
        frame, so the majority of it is still the game's."""
        from lobster.constants import (HIT_TEST_FRAME_BUDGET_US,
                                       SELECTION_PICK_BUDGET_US)
        frame_us = 1e6 / 60.0
        total = (HIT_TEST_FRAME_BUDGET_US + SELECTION_PICK_BUDGET_US
                 + MODEL_DRAW_BUDGET_US)
        self.assertLess(total, frame_us * 0.5,
                        "Lobster's per-frame CPU jobs now take {0:.0f} us of a "
                        "{1:.0f} us frame".format(total, frame_us))

    def test_the_ceilings_are_usable_rather_than_merely_arithmetic(self):
        """A derivation that lands on 3 props per cell is a correct division
        and a useless ceiling, and the answer to that is a cheaper placement -
        not a friendlier number. This is the tripwire for noticing."""
        self.assertGreaterEqual(MAX_PROP_PLACEMENTS_PER_CELL, 16)
        self.assertGreaterEqual(MAX_VISIBLE_PLACEMENTS_PER_FRAME, 150)

    def test_the_modelled_cost_is_the_unit_times_the_count(self):
        self.assertAlmostEqual(modelled_placement_cost_us(10),
                               10 * PER_PLACEMENT_US)
        self.assertEqual(modelled_placement_cost_us(0), 0.0)


class TestPickingAndDrawingDoNotContradictEachOther(unittest.TestCase):
    """`MAX_ITEMS_PER_CELL` is larger than a cell's drawing share, and that is
    not a contradiction - it is two consumers with different worst cases."""

    def test_the_item_ceiling_is_the_larger_one(self):
        self.assertGreater(MAX_ITEMS_PER_CELL, MAX_PROP_PLACEMENTS_PER_CELL,
                           "if drawing ever became the looser of the two, the "
                           "comment in constants.py explaining why picking is "
                           "bigger would be describing the wrong world")

    def test_a_cell_full_of_visible_items_trips_the_frame_budget(self):
        """Which is the loud, attributable failure the two ceilings compose
        into: picking allows more items than drawing can show at once, and the
        frame counter is what says so."""
        self.assertGreater(MAX_ITEMS_PER_CELL * MAX_RESIDENT_CELLS,
                           MAX_VISIBLE_PLACEMENTS_PER_FRAME)


# ---------------------------------------------------------------------------
# The runtime ceiling
# ---------------------------------------------------------------------------

class TestTheFrameCeilingFires(unittest.TestCase):

    def scene(self, count):
        return ResidentCell(plain_bundle(CELL, props=props(count)),
                            Budget.declared(CELL, {}))

    def camera(self):
        return Camera.looking_at((6.0, 6.0, -20.0), (6.0, 0.5, 10.0))

    def draw(self, count, **kwargs):
        return build_draw_list(self.camera(), [self.scene(count)],
                               library=primitive_library(), **kwargs)

    def test_a_frame_under_the_ceiling_is_drawn(self):
        draw_list = self.draw(MAX_VISIBLE_PLACEMENTS_PER_FRAME)
        self.assertEqual(draw_list.stats.placements_drawn,
                         MAX_VISIBLE_PLACEMENTS_PER_FRAME)
        self.assertLessEqual(draw_list.stats.placement_cost_us(),
                             MODEL_DRAW_BUDGET_US)

    def test_one_over_raises_and_names_the_metric(self):
        with self.assertRaises(BudgetViolation) as ctx:
            self.draw(MAX_VISIBLE_PLACEMENTS_PER_FRAME + 1)
        violation = ctx.exception
        self.assertEqual(violation.metric, "model_frame_us")
        self.assertEqual(violation.limit, MODEL_DRAW_BUDGET_US)
        self.assertEqual(violation.cell_id, CELL)
        self.assertIn("fewer things on screen", violation.detail)

    def test_the_violation_speaks_in_microseconds(self):
        with self.assertRaises(BudgetViolation) as ctx:
            self.draw(MAX_VISIBLE_PLACEMENTS_PER_FRAME + 1)
        self.assertAlmostEqual(
            ctx.exception.value,
            round(modelled_placement_cost_us(
                MAX_VISIBLE_PLACEMENTS_PER_FRAME + 1), 1))

    def test_a_prop_with_no_model_is_not_charged(self):
        """It draws as an impostor, which is a different and cheaper path."""
        cell = ResidentCell(plain_bundle(CELL, props=props(500, model_ref="")),
                            Budget.declared(CELL, {}))
        draw_list = build_draw_list(self.camera(), [cell],
                                    library=primitive_library())
        self.assertEqual(draw_list.stats.placements_drawn, 0)
        self.assertTrue(draw_list.of_kind(PROP), "nothing was drawn at all")

    def test_a_culled_placement_is_not_charged(self):
        """Culling is what bounds this, so the count has to be of *visible*
        placements or the ceiling is about authoring rather than about a
        frame."""
        cell = self.scene(200)
        away = Camera.looking_at((6.0, 2.0, 60.0), (6.0, 2.0, 200.0))
        draw_list = build_draw_list(away, [cell], library=primitive_library())
        self.assertEqual(draw_list.stats.placements_drawn, 0)

    def test_zero_disables_it(self):
        """The `HitTester` convention, so a test or a tool can measure without
        being stopped."""
        draw_list = self.draw(MAX_VISIBLE_PLACEMENTS_PER_FRAME + 50,
                              max_visible_placements=0)
        self.assertEqual(draw_list.stats.placements_drawn,
                         MAX_VISIBLE_PLACEMENTS_PER_FRAME + 50)

    def test_the_count_is_in_the_report(self):
        stats = self.draw(20).stats.to_dict()
        self.assertEqual(stats["placements_drawn"], 20)
        self.assertAlmostEqual(stats["placement_cost_us"],
                               modelled_placement_cost_us(20))


# ---------------------------------------------------------------------------
# The build-time ceilings
# ---------------------------------------------------------------------------

class TestThePerCellCeiling(unittest.TestCase):

    def test_it_is_a_declarable_budget_like_every_other(self):
        budget = Budget.declared(CELL, {})
        self.assertEqual(budget.max_prop_placements,
                         MAX_PROP_PLACEMENTS_PER_CELL)
        self.assertIn("max_prop_placements", budget.to_dict())

    def test_a_cell_may_declare_lower(self):
        budget = Budget.declared(
            CELL, {"lobster_budget": {"max_prop_placements": 4}})
        self.assertEqual(budget.max_prop_placements, 4)
        budget.check("max_prop_placements", 4)
        with self.assertRaises(BudgetViolation) as ctx:
            budget.check("max_prop_placements", 5)
        self.assertEqual(ctx.exception.metric, "max_prop_placements")

    def test_a_cell_may_not_declare_higher(self):
        with self.assertRaises(BudgetViolation):
            Budget.declared(CELL, {"lobster_budget": {
                "max_prop_placements": MAX_PROP_PLACEMENTS_PER_CELL + 1}})


class TestTheBuildEnforcesThePerCellCeiling(unittest.TestCase):

    def build(self, count):
        import os
        from lobster.build.builder import build_from_file
        from tests.fixtures import (BuildWorkspace, C, cube_vox,
                                    demo_manifest_cells, demo_world_ops,
                                    slab_vox)
        with BuildWorkspace() as ws:
            slab_vox(os.path.join(ws.art, "slab.vox"))
            cube_vox(os.path.join(ws.art, "cube.vox"))
            ops = demo_world_ops() + [C("model-crate", "Model", primitive={
                "shape": "box", "size": [0.8, 0.8, 0.8], "material": 6})]
            package = ws.write_package("world.props", ops)
            cells = demo_manifest_cells(props=[
                {"prop_id": "p%d" % i, "model_ref": "model-crate",
                 "transform": {"position": [4 + i * 0.1, 0, 4]}}
                for i in range(count)])
            path = ws.write_manifest(cells, [package])
            report = build_from_file(path, out_dir=ws.out, write=False)
            return [f for f in report.findings if f["code"] == "over_budget"]

    def test_a_cell_at_its_ceiling_is_clean(self):
        self.assertEqual(self.build(MAX_PROP_PLACEMENTS_PER_CELL), [])

    def test_one_prop_over_is_reported_and_names_the_metric(self):
        findings = self.build(MAX_PROP_PLACEMENTS_PER_CELL + 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("max_prop_placements", findings[0]["detail"])
        self.assertEqual(findings[0]["cell_id"], "cell-a")

    def test_props_with_no_model_are_not_counted(self):
        """They draw as impostors, which is the cheaper path the frame budget
        does not charge for either."""
        import os
        from lobster.build.builder import build_from_file
        from tests.fixtures import (BuildWorkspace, cube_vox,
                                    demo_manifest_cells, demo_world_ops,
                                    slab_vox)
        with BuildWorkspace() as ws:
            slab_vox(os.path.join(ws.art, "slab.vox"))
            cube_vox(os.path.join(ws.art, "cube.vox"))
            package = ws.write_package("world.props", demo_world_ops())
            cells = demo_manifest_cells(props=[
                {"prop_id": "p%d" % i, "model_ref": "",
                 "transform": {"position": [4 + i * 0.1, 0, 4]}}
                for i in range(MAX_PROP_PLACEMENTS_PER_CELL * 3)])
            path = ws.write_manifest(cells, [package])
            report = build_from_file(path, out_dir=ws.out, write=False)
        self.assertEqual([f for f in report.findings
                          if f["code"] == "over_budget"], [])


class TestTheLibraryCeiling(unittest.TestCase):

    def library_of(self, total_bytes):
        from lobster.constants import MODEL_VERTEX_STRIDE
        per = MODEL_VERTEX_STRIDE * 3
        count = max(1, total_bytes // per)
        return ModelLibrary(models={
            "model-%d" % i: ModelMesh(model_ref="model-%d" % i, kind="voxel",
                                      vertices=b"\0" * per)
            for i in range(count)})

    def test_a_library_within_its_share_is_clean(self):
        from lobster.build.builder import check_library_budget
        self.assertEqual(check_library_budget(primitive_library()), [])

    def test_a_library_over_its_share_fails_the_build(self):
        from lobster.build.builder import check_library_budget
        from lobster.build.lint import ERROR_CODES
        findings = check_library_budget(
            self.library_of(MAX_MODEL_LIBRARY_BYTES + 4096))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "model_library_over_budget")
        self.assertIn("model_library_over_budget", ERROR_CODES)

    def test_the_finding_names_the_biggest_models(self):
        """"19 MiB is too much" is not actionable; "these four are 14 MiB of
        it" is."""
        from lobster.build.builder import check_library_budget
        huge = ModelLibrary(models={
            "model-small": ModelMesh(model_ref="model-small", kind="voxel",
                                     vertices=b"\0" * 108),
            "model-enormous": ModelMesh(
                model_ref="model-enormous", kind="voxel",
                vertices=b"\0" * (MAX_MODEL_LIBRARY_BYTES + 108))})
        detail = check_library_budget(huge)[0]["detail"]
        self.assertIn("model-enormous", detail)
        self.assertIn(str(MAX_MODEL_LIBRARY_BYTES), detail)


class TestOctopusOwnCostHookIsHonoured(unittest.TestCase):
    """§6: `cost_estimate` "should be honoured rather than re-derived"."""

    class FakeView:
        def __init__(self, records):
            self.records = records

        def records_of_type(self, type_name):
            return list(self.records)

    class FakeManifest:
        models = ()
        cells = ()
        source_path = None

        def model(self, model_ref):
            return None

    def findings_for(self, record):
        from lobster.build.library_writer import build_library
        _library, findings = build_library(self.FakeView([record]),
                                           self.FakeManifest())
        return findings

    def spec(self, **extra):
        record = {"id": "model-crate", "type": "Model",
                  "primitive": {"shape": "box", "size": [0.8, 0.8, 0.8],
                                "material": 6}}
        record.update(extra)
        return record

    def test_an_estimate_the_geometry_has_outgrown_is_reported(self):
        findings = self.findings_for(self.spec(cost_estimate=8))
        self.assertEqual([f["code"] for f in findings],
                         ["model_cost_estimate_low"])
        self.assertEqual(findings[0]["record_id"], "model-crate")
        self.assertIn("1296", findings[0]["detail"])

    def test_a_generous_estimate_is_not_a_finding(self):
        self.assertEqual(self.findings_for(self.spec(cost_estimate=100_000)), [])

    def test_no_estimate_is_not_a_finding(self):
        """Zero is Octopus's default for the field, so it means "not declared"
        rather than "declared as nothing"."""
        self.assertEqual(self.findings_for(self.spec()), [])
        self.assertEqual(self.findings_for(self.spec(cost_estimate=0)), [])

    def test_it_does_not_fail_the_build(self):
        from lobster.build.lint import ERROR_CODES
        self.assertNotIn("model_cost_estimate_low", ERROR_CODES)


if __name__ == "__main__":
    unittest.main()
