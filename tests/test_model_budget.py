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
from lobster.constants import (DEFAULT_MAX_CELL_BYTES, EXTERIOR_TAG,
                               MAX_DRAWABLE_PLACEMENTS_PER_CELL,
                               MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR,
                               MAX_ITEMS_PER_CELL, MAX_MODEL_LIBRARY_BYTES,
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

    def test_an_exterior_cell_gets_the_frame_over_its_ring(self):
        self.assertEqual(MAX_DRAWABLE_PLACEMENTS_PER_CELL,
                         int(MAX_VISIBLE_PLACEMENTS_PER_FRAME
                             / MAX_RESIDENT_CELLS))

    def test_an_interior_gets_the_whole_frame_because_it_is_alone(self):
        """`residency_ring`: "an exterior cell brings its exterior neighbours;
        an interior brings nothing". Dividing by the ring anyway charged a
        player's house for eight neighbours it can never have (D52)."""
        self.assertEqual(MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR,
                         MAX_VISIBLE_PLACEMENTS_PER_FRAME)
        self.assertGreater(MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR,
                           MAX_DRAWABLE_PLACEMENTS_PER_CELL)

    def test_the_ring_rule_here_is_the_one_the_loader_uses(self):
        """Two definitions of "what is resident with me" would make this
        ceiling a statement about a different world than the one that loads."""
        from lobster.cell import is_exterior, residency_ring

        class View:
            def __init__(self, tags):
                self.tags = tags

            def record(self, cell_id):
                return {"id": cell_id, "tags": self.tags}

            def connections(self, cell_id):
                return []

        interior, exterior = View([]), View([EXTERIOR_TAG])
        self.assertFalse(is_exterior(interior, "home"))
        self.assertEqual(residency_ring(interior, "home"), ["home"],
                         "an interior brings nothing, so its ceiling is the "
                         "whole frame")
        self.assertTrue(is_exterior(exterior, "field"))

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
        """A derivation that lands on 3 things per cell is a correct division
        and a useless ceiling, and the answer to that is a cheaper placement -
        not a friendlier number. This is the tripwire for noticing.

        The interior floor is the one that matters for content: a player home
        with 34 things in it is not a house, which is how D52 got found.
        """
        self.assertGreaterEqual(MAX_DRAWABLE_PLACEMENTS_PER_CELL, 16)
        self.assertGreaterEqual(MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR, 150)
        self.assertGreaterEqual(MAX_VISIBLE_PLACEMENTS_PER_FRAME, 150)

    def test_the_modelled_cost_is_the_unit_times_the_count(self):
        self.assertAlmostEqual(modelled_placement_cost_us(10),
                               10 * PER_PLACEMENT_US)
        self.assertEqual(modelled_placement_cost_us(0), 0.0)


class TestPickingAndDrawingAreDifferentCosts(unittest.TestCase):
    """`MAX_ITEMS_PER_CELL` counts items with or without a mesh; the drawing
    ceiling counts meshes whether they are items or props. Neither contains the
    other, and the first version of this file claimed they composed when they
    did not (D52)."""

    def test_the_ring_of_exterior_cells_fits_the_frame(self):
        """The property the per-cell ceiling exists to guarantee, and the one
        the first derivation did not: nine exterior cells at their ceiling,
        everything on screen, still inside the frame budget."""
        self.assertLessEqual(
            MAX_DRAWABLE_PLACEMENTS_PER_CELL * MAX_RESIDENT_CELLS,
            MAX_VISIBLE_PLACEMENTS_PER_FRAME)

    def test_an_interior_alone_fits_the_frame(self):
        self.assertLessEqual(MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR,
                             MAX_VISIBLE_PLACEMENTS_PER_FRAME)

    def test_a_cell_may_hold_more_items_than_it_can_draw(self):
        """And that is legal: an item with no `model_ref` is an impostor, which
        neither ceiling charges for."""
        self.assertGreater(MAX_ITEMS_PER_CELL,
                           MAX_DRAWABLE_PLACEMENTS_PER_CELL)


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

    def exterior(self, **extra):
        record = {"tags": [EXTERIOR_TAG]}
        record.update(extra)
        return record

    def test_the_default_follows_the_cell_kind(self):
        self.assertEqual(Budget.declared(CELL, {}).max_drawable_placements,
                         MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR)
        self.assertEqual(
            Budget.declared(CELL, self.exterior()).max_drawable_placements,
            MAX_DRAWABLE_PLACEMENTS_PER_CELL)
        self.assertIn("max_drawable_placements",
                      Budget.declared(CELL, {}).to_dict())

    def test_an_untagged_location_is_an_interior(self):
        """Which is what `cell.is_exterior` already means by it - one
        definition, not two."""
        self.assertEqual(
            Budget.declared(CELL, {"tags": ["indoor", "shop"]})
            .max_drawable_placements,
            MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR)

    def test_a_cell_may_declare_lower(self):
        budget = Budget.declared(
            CELL, {"lobster_budget": {"max_drawable_placements": 4}})
        self.assertEqual(budget.max_drawable_placements, 4)
        budget.check("max_drawable_placements", 4)
        with self.assertRaises(BudgetViolation) as ctx:
            budget.check("max_drawable_placements", 5)
        self.assertEqual(ctx.exception.metric, "max_drawable_placements")

    def test_a_cell_may_not_declare_higher(self):
        with self.assertRaises(BudgetViolation):
            Budget.declared(CELL, {"lobster_budget": {
                "max_drawable_placements":
                    MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR + 1}})

    def test_an_exterior_may_not_declare_the_interior_ceiling(self):
        """The ceiling a cell may not exceed is *its own*, not the largest one
        in the file."""
        with self.assertRaises(BudgetViolation) as ctx:
            Budget.declared(CELL, self.exterior(lobster_budget={
                "max_drawable_placements":
                    MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR}))
        self.assertEqual(ctx.exception.limit, MAX_DRAWABLE_PLACEMENTS_PER_CELL)


class TestTheBuildEnforcesThePerCellCeiling(unittest.TestCase):

    #: the demo world's cells are tagged `exterior`, so the ceiling under test
    #: here is the ring-divided one
    CEILING = MAX_DRAWABLE_PLACEMENTS_PER_CELL

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
        self.assertEqual(self.build(self.CEILING), [])

    def test_one_prop_over_is_reported_and_names_the_metric(self):
        findings = self.build(self.CEILING + 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("max_drawable_placements", findings[0]["detail"])
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
                for i in range(MAX_DRAWABLE_PLACEMENTS_PER_CELL * 3)])
            path = ws.write_manifest(cells, [package])
            report = build_from_file(path, out_dir=ws.out, write=False)
        self.assertEqual([f for f in report.findings
                          if f["code"] == "over_budget"], [])


class TestPropsAndItemsShareTheCeiling(unittest.TestCase):
    """The defect D52 records: props were counted and items were not, while
    both fed the same per-frame counter."""

    def world(self, props, items, item_cell="cell-a"):
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
            ops += [C("item-%d" % i, "Item", display_name="Thing %d" % i,
                      model_ref="model-crate",
                      default_location_ref=item_cell,
                      world_transform={"position": [1 + i * 0.1, 0, 1],
                                       "rotation": [0, 0, 0, 1]})
                    for i in range(items)]
            package = ws.write_package("world.mix", ops)
            cells = demo_manifest_cells(props=[
                {"prop_id": "p%d" % i, "model_ref": "model-crate",
                 "transform": {"position": [4 + i * 0.1, 0, 4]}}
                for i in range(props)])
            path = ws.write_manifest(cells, [package])
            report = build_from_file(path, out_dir=ws.out, write=False)
            return [f for f in report.findings if f["code"] == "over_budget"]

    def test_props_and_items_are_counted_together(self):
        half = MAX_DRAWABLE_PLACEMENTS_PER_CELL // 2
        self.assertEqual(self.world(half, MAX_DRAWABLE_PLACEMENTS_PER_CELL
                                    - half), [])
        findings = self.world(half, MAX_DRAWABLE_PLACEMENTS_PER_CELL - half + 1)
        self.assertEqual(len(findings), 1,
                         "props and items were counted separately, so a cell "
                         "under each ceiling and over their sum passed")
        self.assertIn("max_drawable_placements", findings[0]["detail"])

    def test_items_are_attributed_to_the_cell_they_are_in(self):
        """A count that forgot to filter by cell would charge every cell in the
        world for every item in it - which passes the test above, because the
        cell that was already over stays over."""
        findings = self.world(0, MAX_DRAWABLE_PLACEMENTS_PER_CELL + 1,
                              item_cell="cell-b")
        self.assertEqual([f["cell_id"] for f in findings], ["cell-b"],
                         "the items are in cell-b; cell-a holds none of them "
                         "and must not be charged for them")

    def test_an_item_with_no_model_is_not_counted(self):
        """It draws as an impostor, and a cell may hold more items than it can
        draw - that is what keeps `MAX_ITEMS_PER_CELL` meaningful."""
        import os
        from lobster.build.builder import build_from_file
        from tests.fixtures import (BuildWorkspace, C, cube_vox,
                                    demo_manifest_cells, demo_world_ops,
                                    slab_vox)
        with BuildWorkspace() as ws:
            slab_vox(os.path.join(ws.art, "slab.vox"))
            cube_vox(os.path.join(ws.art, "cube.vox"))
            ops = demo_world_ops() + [
                C("item-%d" % i, "Item", display_name="Thing %d" % i,
                  default_location_ref="cell-a",
                  world_transform={"position": [1 + i * 0.1, 0, 1],
                                   "rotation": [0, 0, 0, 1]})
                for i in range(MAX_DRAWABLE_PLACEMENTS_PER_CELL * 2)]
            package = ws.write_package("world.plain", ops)
            path = ws.write_manifest(demo_manifest_cells(), [package])
            report = build_from_file(path, out_dir=ws.out, write=False)
        self.assertEqual([f for f in report.findings
                          if f["code"] == "over_budget"], [])


class TestPlacingAnItemRespectsTheDrawingCeiling(unittest.TestCase):
    """`place_item` already refuses a cell at its `max_items`. It refuses one at
    its drawing ceiling for the same reason and in the same place - before the
    write, so a refusal leaves nothing behind (D36, D52)."""

    def setUp(self):
        from lobster.cell import CellManager
        from lobster.events import EventBus
        from lobster.octopus_bridge import OctopusBridge
        from tests.fixtures import (BundleWorkspace, VILLAGE, build_session,
                                    plain_bundle, prop)
        self.VILLAGE = VILLAGE
        self.session = build_session()
        for i in range(4):
            self.session.engine.write({"op": "CREATE", "record": {
                "id": "item-%d" % i, "type": "Item",
                "display_name": "Thing %d" % i,
                "model_ref": "model-crate"}})
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-plain", "type": "Item", "display_name": "Plain"}})
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "model-crate", "type": "Model"}})
        self.bridge = OctopusBridge(self.session)
        self.ws = BundleWorkspace()
        self.addCleanup(self.ws.close)
        # two props with meshes, and a ceiling of three, so the third item is
        # one too many
        self.ws.write(plain_bundle(VILLAGE, props=[prop("a", "model-crate"),
                                                   prop("b", "model-crate")]))
        self.manager = CellManager(self.ws.path, bus=EventBus(),
                                   session=self.session)
        view = self.bridge.frame()
        self.manager.load(view, VILLAGE)
        self.manager.resident[VILLAGE].budget = Budget.declared(
            VILLAGE, {"lobster_budget": {"max_drawable_placements": 3}})

    def place(self, item_id):
        self.manager.place_item(self.bridge.frame(), item_id, self.VILLAGE,
                                Transform(position=(6.0, 0.0, 8.0)))

    def test_the_props_already_count_against_it(self):
        cell = self.manager.resident[self.VILLAGE]
        self.assertEqual(cell.drawable_placements(), 2)

    def test_one_more_meshed_item_fits_and_the_next_does_not(self):
        self.place("item-0")
        with self.assertRaises(BudgetViolation) as ctx:
            self.place("item-1")
        self.assertEqual(ctx.exception.metric, "max_drawable_placements")
        self.assertEqual(ctx.exception.record_id, "item-1")
        self.assertIn("impostor", ctx.exception.detail)

    def test_a_refusal_leaves_nothing_behind(self):
        self.place("item-0")
        try:
            self.place("item-1")
        except BudgetViolation:
            pass
        placed = [r["id"] for r in
                  self.bridge.frame().items_in_location(self.VILLAGE)]
        self.assertEqual(placed, ["item-0"],
                         "a refused placement wrote a record anyway")

    def test_an_item_with_no_mesh_is_never_refused(self):
        """A cell may hold more items than it can draw; an impostor costs the
        draw budget nothing."""
        self.place("item-0")
        self.place("item-plain")
        placed = sorted(r["id"] for r in
                        self.bridge.frame().items_in_location(self.VILLAGE))
        self.assertEqual(placed, ["item-0", "item-plain"])


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
