"""GPU buffer lifetime driven by residency Events (RENDER_SCOPE §4, step 3).

The scope calls this "the real design work", and the design is mostly a
question of *who calls whom*: `CellManager.load` invoking a render backend
would put presentation inside the geometry layer, so the backend subscribes to
the residency Events instead and nothing gains an import.

What is asserted here is the property the whole `upload_cell` interface exists
for — **static geometry reaches the GPU once per residency, not once per
frame** — plus the pairing that stops it leaking.
"""

from __future__ import annotations

import unittest

from lobster.cell import CellManager
from lobster.events import EventBus
from lobster.geometry import Transform
from lobster.octopus_bridge import OctopusBridge
from lobster.render.recording import RecordingBackend
from lobster.render.residency import GpuResidency, ResidencyError
from tests.fixtures import (FAR_CELL, FIELD, GATEHOUSE, HOME_CELL, KEEP,
                            VILLAGE, build_session, disjoint_world,
                            standard_workspace)


class ResidencyFixture(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.bus = EventBus()
        self.manager = CellManager(self.ws.path, bus=self.bus,
                                   session=self.session)
        self.backend = RecordingBackend()
        self.gpu = GpuResidency(self.backend, self.manager).attach(self.bus)

    def view(self):
        return self.bridge.frame()


class TestUploadsFollowResidency(ResidencyFixture):

    def test_loading_a_cell_uploads_it(self):
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.assertIn(VILLAGE, self.backend.uploaded)
        self.assertEqual(sorted(self.backend.live_cells()),
                         sorted(self.manager.resident))

    def test_the_whole_ring_is_uploaded_not_just_the_player_cell(self):
        """`upload_cell` follows residency, and residency is the ring (D8)."""
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.assertIn(FIELD, self.backend.uploaded,
                      "the neighbour is resident, so its buffers belong on the "
                      "GPU too - it is what you see across the boundary")

    def test_unloading_releases_it(self):
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.manager.set_player_cell(self.view(), KEEP)
        self.assertIn(FIELD, self.backend.released)
        self.assertNotIn(FIELD, self.backend.live_cells())

    def test_geometry_reaches_the_gpu_once_per_residency_not_once_per_frame(self):
        """The property the interface exists for. Ten frames, one upload."""
        self.manager.set_player_cell(self.view(), VILLAGE)
        for _frame in range(10):
            self.manager.set_player_cell(self.view(), VILLAGE)
        self.assertEqual(self.backend.upload_count(VILLAGE), 1,
                         "a re-entered cell was uploaded again; `load` returns "
                         "early when resident and must not re-fire")

    def test_a_cell_kept_across_a_move_is_not_churned(self):
        """Stepping between two exterior neighbours keeps both resident, so
        neither should be released and re-uploaded - that is a stall for no
        reason.

        Deliberately *not* village -> keep, which was this test's first draft:
        the keep is an interior and interiors bring no ring (D8), so the
        village correctly unloads and the assertion was about a case that does
        not exist.
        """
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.manager.set_player_cell(self.view(), FIELD)
        self.assertIn(VILLAGE, self.manager.resident,
                      "fixture must actually keep it resident")
        self.assertEqual(self.backend.upload_count(VILLAGE), 1)
        self.assertNotIn(VILLAGE, self.backend.released)


class TestItStaysInStep(ResidencyFixture):
    """`drift()` is the leak check, and D43 is a fresh reminder that resource
    bugs pass every test that only looks at answers."""

    def test_nothing_drifts_across_a_walk(self):
        for destination in (VILLAGE, KEEP, VILLAGE, FIELD):
            self.manager.set_player_cell(self.view(), destination)
            self.assertTrue(self.gpu.in_step(),
                            "after {0}: {1}".format(destination,
                                                    self.gpu.drift()))

    def test_nothing_drifts_across_a_jump(self):
        """Fast travel releases the old ring *before* loading the new one
        (D32), so the release handler cannot consult `manager.resident` - the
        answer would be about the wrong moment. This is why the binding keeps
        its own set."""
        session, ws, _ids = disjoint_world()
        self.addCleanup(ws.close)
        bridge = OctopusBridge(session)
        bus = EventBus()
        manager = CellManager(ws.path, bus=bus, session=session)
        backend = RecordingBackend()
        gpu = GpuResidency(backend, manager).attach(bus)

        manager.set_player_cell(bridge.frame(), HOME_CELL)
        self.assertTrue(gpu.in_step(), gpu.drift())
        manager.set_player_cell(bridge.frame(), FAR_CELL)
        self.assertTrue(gpu.in_step(), gpu.drift())
        self.assertEqual(sorted(backend.live_cells()),
                         sorted(manager.resident))

    def test_every_upload_is_eventually_released(self):
        self.manager.set_player_cell(self.view(), VILLAGE)
        for cell_id in list(self.manager.resident):
            self.manager.unload(cell_id)
        self.assertEqual(self.backend.live_cells(), [],
                         "cells uploaded and never released are GPU memory "
                         "nobody can reclaim")
        self.assertTrue(self.gpu.in_step(), self.gpu.drift())

    def test_drift_names_both_directions(self):
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.gpu.uploaded.add("cell-that-never-was")
        drift = self.gpu.drift()
        self.assertIn("cell-that-never-was", drift["leaked"])
        self.assertEqual(drift["missing"], [])


class TestBreakStateIsTheDynamicCase(ResidencyFixture):
    """Static per residency, except that destroying a chunk moves structure
    geometry on the frame the wall comes down."""

    def test_damage_re_uploads_the_structure(self):
        self.manager.set_player_cell(self.view(), VILLAGE)
        before = self.backend.upload_count(VILLAGE)
        self.manager.damage_structure(self.view(), VILLAGE, GATEHOUSE, [0, 1])

        self.assertEqual(len(self.backend.structure_uploads), 1)
        cell_id, structure_id, chunks = self.backend.structure_uploads[0]
        self.assertEqual((cell_id, structure_id), (VILLAGE, GATEHOUSE))
        self.assertEqual(chunks, (0, 1))
        self.assertEqual(self.backend.upload_count(VILLAGE), before,
                         "a damaged wall must re-upload the structure, not the "
                         "whole cell")

    def test_damage_to_a_structure_in_no_uploaded_cell_is_ignored(self):
        """An offscreen siege damages structures in cells nobody has loaded
        (§6.5). There is no buffer to refresh and asking for one would raise."""
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.bus.structure_damaged("keep-somewhere-else", [3])
        self.assertEqual(self.backend.structure_uploads, [])


class TestTheHandlersRefuseWhatTheyShould(ResidencyFixture):

    def test_an_enter_event_for_a_cell_that_is_not_resident_raises(self):
        """`load` inserts before it fires, so this means something other than
        residency raised the Event - and uploading geometry for a cell nobody
        can see would leak it."""
        with self.assertRaises(ResidencyError) as ctx:
            self.bus.enter_cell("cell-nowhere")
        self.assertIn("not resident", str(ctx.exception))

    def test_a_replayed_enter_event_is_counted_not_obeyed(self):
        """The bus is public. A caller replaying an Event is not a crime, but
        it must not double-upload."""
        self.manager.set_player_cell(self.view(), VILLAGE)
        self.bus.enter_cell(VILLAGE)
        self.assertEqual(self.backend.upload_count(VILLAGE), 1)
        self.assertEqual(self.gpu.stats.duplicate_uploads_suppressed, 1)

    def test_an_exit_for_something_never_uploaded_is_counted_not_obeyed(self):
        self.bus.exit_cell("cell-nowhere")
        self.assertEqual(self.backend.released, [])
        self.assertEqual(self.gpu.stats.unknown_releases, 1)


class TestNothingElseLearnedAboutRendering(unittest.TestCase):
    """The reason this went through the bus rather than through `CellManager`."""

    def test_cell_py_still_imports_no_renderer(self):
        import inspect
        import re
        from lobster import cell
        source = inspect.getsource(cell)
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        for banned in ("render", "backend", "moderngl", "upload_cell"):
            self.assertNotIn(banned, code,
                             "cell.py mentions {0!r} - residency is geometry, "
                             "and reaching into presentation is the L8 mistake "
                             "at a different boundary".format(banned))

    def test_the_binding_imports_no_cell_module(self):
        """It takes a manager as an argument and duck-types it, so the
        dependency runs one way only."""
        import inspect
        from lobster.render import residency
        source = inspect.getsource(residency)
        self.assertNotIn("from ..cell import", source)
        self.assertNotIn("import cell", source)


if __name__ == "__main__":
    unittest.main()
