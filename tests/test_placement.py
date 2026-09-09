"""Exterior cell placement and renderer backends (DECISIONS.md D21, D22).

Two decisions taken together, because the first is what makes the second worth
having: until exterior cells knew where they were, a renderer could only ever
draw one cell and the world stopped at the boundary.

* **D21** - an exterior `Location` declares `exterior_grid: [x, z]` and is
  placed at `[x * 128, 0, z * 128]`. Interiors are their own coordinate space
  and are placed nowhere, which is what makes the Morrowind cell model cheap.
* **D22** - Lobster uses the GPU when it is available, and falls back to the
  software rasteriser when it is not, saying which and why.
"""

from __future__ import annotations

import unittest

from lobster.build.lint import ERROR_CODES, check_exterior_grid, errors
from lobster.camera import Camera
from lobster.cell import (CellError, CellManager, cell_placement,
                          exterior_grid)
from lobster.constants import EXTERIOR_CELL_SIZE_M
from lobster.geometry import Transform
from lobster.octopus_bridge import OctopusBridge
from lobster.render import (BackendError, RenderSettings, SoftwareBackend,
                            probe, render_resident, select_backend)
from lobster.render.backend import probe_with, selection_report
from lobster.visibility import build_draw_list
from tests.fixtures import (FIELD, KEEP, VILLAGE, build_session, package_dict,
                            standard_workspace)


def grid_ops(**cells):
    return package_dict("mod.grid", [
        {"op": "PATCH", "id": cell_id, "field": "exterior_grid", "value": value}
        for cell_id, value in cells.items()])


def view_with(**cells):
    return OctopusBridge(build_session(
        extra_packages=[grid_ops(**cells)] if cells else [])).frame()


class TestExteriorPlacement(unittest.TestCase):

    def test_the_fixture_world_is_placed(self):
        view = view_with()
        self.assertEqual(exterior_grid(view, VILLAGE), (0, 0))
        self.assertEqual(exterior_grid(view, FIELD), (0, 1))

    def test_grid_coordinates_become_a_world_offset(self):
        view = view_with(**{VILLAGE: [3, 4]})
        self.assertEqual(cell_placement(view, VILLAGE).position,
                         (3 * EXTERIOR_CELL_SIZE_M, 0.0,
                          4 * EXTERIOR_CELL_SIZE_M))

    def test_an_interior_is_placed_nowhere(self):
        """An interior *is* its own space - placing it would invent a fact."""
        view = view_with()
        self.assertIsNone(exterior_grid(view, KEEP))
        self.assertEqual(cell_placement(view, KEEP), Transform())

    def test_there_is_no_vertical_offset(self):
        """Elevation is the heightfield's job; a per-cell Y would be a second
        way to say the same thing."""
        view = view_with(**{VILLAGE: [2, 2]})
        self.assertEqual(cell_placement(view, VILLAGE).position[1], 0.0)

    def test_a_malformed_grid_is_refused_by_name(self):
        view = view_with(**{VILLAGE: [1, 2, 3]})
        with self.assertRaises(CellError) as ctx:
            exterior_grid(view, VILLAGE)
        self.assertIn(VILLAGE, str(ctx.exception))
        self.assertIn("two integers", str(ctx.exception))

    def test_neighbours_are_a_cell_apart(self):
        view = view_with()
        village = cell_placement(view, VILLAGE).position
        field = cell_placement(view, FIELD).position
        self.assertEqual(field[2] - village[2], EXTERIOR_CELL_SIZE_M)

    def test_the_manager_supplies_placements_for_what_is_resident(self):
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            view = bridge.frame()
            manager.set_player_cell(view, VILLAGE)
            placements = manager.placements(view)
            self.assertEqual(sorted(placements), sorted(manager.resident))
            self.assertEqual(placements[FIELD].position,
                             (0.0, 0.0, EXTERIOR_CELL_SIZE_M))

    def test_placed_cells_draw_as_one_world(self):
        """The point of the whole decision: a neighbour is somewhere to draw."""
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            view = bridge.frame()
            manager.set_player_cell(view, VILLAGE)
            cells = [manager.resident[c] for c in sorted(manager.resident)]

            camera = Camera.looking_at((60.0, 40.0, -60.0), (60.0, 0.0, 90.0))
            placed = build_draw_list(camera, cells,
                                     placements=manager.placements(view))
            stacked = build_draw_list(camera, cells)     # no placements

            self.assertEqual(len(placed.cells()), 2,
                             "both cells should be visible once placed apart")
            self.assertNotEqual(
                sorted(i.center for i in placed.items),
                sorted(i.center for i in stacked.items),
                "without placements the two cells sit on top of each other")

    def test_rendering_the_resident_set_works_end_to_end(self):
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            view = bridge.frame()
            manager.set_player_cell(view, VILLAGE)
            camera = Camera.looking_at((60.0, 30.0, -30.0), (60.0, 0.0, 60.0))
            frame = render_resident(camera, manager, view,
                                    settings=RenderSettings(width=160,
                                                            height=90))
            self.assertGreater(frame.pixels_written, 0)


class TestExteriorGridLint(unittest.TestCase):

    def codes(self, **cells):
        return [f["code"] for f in check_exterior_grid(view_with(**cells))]

    def test_a_placed_world_lints_clean(self):
        self.assertEqual(self.codes(), [])

    def test_a_connected_exterior_with_no_grid_is_an_error(self):
        package = package_dict("mod.unplace", [
            {"op": "PATCH", "id": FIELD, "field": "exterior_grid",
             "value": None}])
        view = OctopusBridge(build_session(extra_packages=[package])).frame()
        findings = check_exterior_grid(view)
        self.assertEqual([f["record_id"] for f in findings], [FIELD])
        self.assertEqual(findings[0]["code"], "exterior_without_grid")
        self.assertTrue(errors(findings), "this must fail a build")

    def test_two_cells_cannot_share_a_square(self):
        self.assertIn("exterior_grid_collision",
                      self.codes(**{VILLAGE: [2, 2], FIELD: [2, 2]}))

    def test_connected_cells_far_apart_are_a_warning(self):
        codes = self.codes(**{VILLAGE: [0, 0], FIELD: [9, 9]})
        self.assertIn("exterior_grid_not_adjacent", codes)
        self.assertNotIn("exterior_grid_not_adjacent", ERROR_CODES,
                         "a long road or a ferry is legitimate content")

    def test_an_interior_needs_no_grid(self):
        """The keep is connected to an exterior and still needs nothing."""
        self.assertEqual(
            [f for f in check_exterior_grid(view_with())
             if f["record_id"] == KEEP], [])


class TestRenderBackends(unittest.TestCase):
    """D22 - GPU when available, software when not, and say which."""

    def test_the_software_backend_is_always_available(self):
        info = SoftwareBackend.available()
        self.assertTrue(info.available)
        self.assertIn("standard library", info.detail)

    def test_probe_reports_every_backend_and_why(self):
        infos = {i.name: i for i in probe()}
        self.assertIn("software", infos)
        self.assertIn("moderngl", infos)
        for info in infos.values():
            self.assertTrue(info.detail,
                            "a backend that cannot run must say why")

    def test_selection_falls_back_to_software_here(self):
        """No GPU, no display: the fallback is the whole point."""
        self.assertEqual(select_backend().name, "software")

    def test_asking_for_a_backend_that_cannot_run_raises(self):
        """Silently handing back a Python rasteriser to somebody who asked for
        the GPU would draw the right picture and blame the wrong thing."""
        with self.assertRaises(BackendError) as ctx:
            select_backend("moderngl")
        self.assertIn("moderngl", str(ctx.exception))
        self.assertIn("cannot run here", str(ctx.exception))

    def test_an_unknown_backend_lists_what_there_is(self):
        with self.assertRaises(BackendError) as ctx:
            select_backend("vulkan")
        self.assertIn("software", str(ctx.exception))

    def test_the_chosen_backend_carries_why_the_better_ones_were_skipped(self):
        """Silent fallback is how somebody spends an afternoon profiling the
        wrong layer (D29). The chain travels with the backend."""
        backend = select_backend()
        report = backend.selection
        self.assertEqual(report.chosen, "software")
        self.assertEqual([name for name, _ in report.skipped],
                         ["moderngl", "moderngl-llvmpipe"])
        for _name, reason in report.skipped:
            self.assertTrue(reason, "a skipped tier must say why")
        self.assertEqual(
            report.summary(),
            "moderngl unavailable -> moderngl-llvmpipe unavailable -> software")


class TestTheThreeTierChain(unittest.TestCase):
    """D29, tested where this machine cannot go.

    The build box has no graphics stack at all, so `probe()` can only ever
    exercise the "no GL" branch. `probe_with()` takes the `GL_RENDERER` string
    as an argument precisely so the other two branches are executable here -
    reasoning about a fallback chain instead of running it is the D22 mistake.
    """

    def chain(self, renderer):
        return selection_report(infos=probe_with(renderer))

    def test_no_gl_at_all_lands_on_pure_python(self):
        report = self.chain(None)
        self.assertEqual(report.chosen, "software")
        self.assertEqual(len(report.skipped), 2)
        for _name, reason in report.skipped:
            self.assertIn("no OpenGL", reason)

    def test_llvmpipe_is_recognised_as_the_middle_tier(self):
        """Mesa's software rasteriser is GL, so it must not be mistaken for a
        GPU - and it must not be skipped past to pure Python either."""
        for renderer in ("llvmpipe (LLVM 15.0.7, 256 bits)",
                         "softpipe", "Software Rasterizer", "swrast"):
            infos = {i.name: i for i in probe_with(renderer)}
            self.assertFalse(infos["moderngl"].available, renderer)
            self.assertIn("software-rasterised", infos["moderngl"].detail)
            # available() is False here only because the GL backend is not
            # written; the *tier detection* is what this asserts.
            self.assertIn(renderer, infos["moderngl-llvmpipe"].detail)

    def test_hardware_gl_is_the_top_tier(self):
        infos = {i.name: i for i in probe_with("NVIDIA GeForce RTX 4070")}
        self.assertIn("hardware GL", infos["moderngl"].detail)
        self.assertFalse(infos["moderngl-llvmpipe"].available,
                         "a hardware context is not the llvmpipe tier")
        self.assertIn("not this tier", infos["moderngl-llvmpipe"].detail)

    def test_the_middle_tier_is_optional_and_absent_without_drama(self):
        """llvmpipe is an OS package, never vendored. Its absence is a
        one-line fallback, not an error."""
        report = self.chain(None)
        self.assertEqual(report.chosen, "software")

    def test_the_pure_python_tier_is_available_in_every_branch(self):
        """Some CI images have Mesa and some do not; the zero-package path has
        to keep working or CI becomes environment-dependent."""
        for renderer in (None, "llvmpipe", "AMD Radeon"):
            infos = {i.name: i for i in probe_with(renderer)}
            self.assertTrue(infos["software"].available, renderer)

    def test_every_tier_always_explains_itself(self):
        for renderer in (None, "llvmpipe", "Intel Iris Xe"):
            for info in probe_with(renderer):
                self.assertTrue(info.detail,
                                "{0} said nothing about {1}".format(
                                    info.name, renderer))

    def test_a_gl_string_is_never_silently_ignored(self):
        """If a machine reports a renderer, the detail has to quote it, or a
        misdetection is undebuggable from the log alone."""
        infos = {i.name: i for i in probe_with("Mali-G78")}
        self.assertIn("Mali-G78", infos["moderngl"].detail)

    def test_core_lobster_imports_with_no_graphics_stack(self):
        """L7: the shell drags in no graphics dependency. This environment has
        none installed, so the whole suite passing is the assertion - but state
        it, so a future `import moderngl` at module scope fails here."""
        import lobster.render as render_pkg
        import lobster.camera, lobster.visibility
        for module in (render_pkg, lobster.camera, lobster.visibility):
            source_file = module.__file__
            with open(source_file, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith(("import ", "from ")):
                        for banned in ("moderngl", "pyglet", "OpenGL",
                                       "pygame", "glfw"):
                            self.assertNotIn(
                                banned, stripped,
                                "{0} imports {1} at module scope".format(
                                    module.__name__, banned))


if __name__ == "__main__":
    unittest.main()
