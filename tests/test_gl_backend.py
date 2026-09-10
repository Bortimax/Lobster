"""The ModernGL backend (RENDER_SCOPE step 4, D22's long-owed answer).

Two layers, and the split is the point of RENDER_SCOPE §5:

* **Draw-call assertions against `RecordingContext`**, which need no GPU and
  run everywhere — the property that geometry reaches the GPU once per
  residency is true regardless of driver, resolution or sun angle.
* **A handful of tests against real hardware**, skipped where there is none.
  Those exist because everything interesting about this backend that went wrong
  went wrong on a real driver and nowhere else.

There is deliberately **no pixel comparison against the software path** (§2).
"""

from __future__ import annotations

import unittest

from lobster.camera import Camera
from lobster.cell import CellManager
from lobster.events import EventBus
from lobster.geometry import Transform
from lobster.octopus_bridge import OctopusBridge
from lobster.render import RenderSettings
from lobster.render.backend import forget_gl_probe, gl_probe, prime_gl_probe
from lobster.render.gl_backend import (ModernGLBackend, VERTEX_STRIDE,
                                       _model_matrix, _identity,
                                       _view_projection)
from lobster.render.recording import RecordingContext
from lobster.render.residency import GpuResidency
from lobster.visibility import ITEM, STRUCTURE, TERRAIN, build_draw_list
from tests.fixtures import (FIELD, GATEHOUSE, VILLAGE, build_session,
                            standard_workspace)


def has_gpu() -> bool:
    return gl_probe()[0] is not None


class BackendFixture(unittest.TestCase):
    """A resident world, a backend, and residency wired to it."""

    WIDTH, HEIGHT = 64, 36

    def build(self, context=None):
        self.session = build_session()
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-sword", "type": "Item", "display_name": "Sword"}})
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.bus = EventBus()
        self.manager = CellManager(self.ws.path, bus=self.bus,
                                   session=self.session)
        self.backend = ModernGLBackend(context, width=self.WIDTH,
                                       height=self.HEIGHT)
        GpuResidency(self.backend, self.manager).attach(self.bus)
        view = self.bridge.frame()
        self.manager.set_player_cell(view, VILLAGE)
        self.manager.place_item(view, "item-sword", VILLAGE,
                                Transform(position=(6.0, 0.4, 8.0)))
        self.view = self.bridge.frame()

    def settings(self):
        return RenderSettings(width=self.WIDTH, height=self.HEIGHT)

    def draw_list(self, eye=(6.0, 1.5, 2.0), target=(6.0, 0.5, 10.0)):
        settings = self.settings()
        camera = Camera.looking_at(eye, target).with_aspect(settings.aspect())
        cells = [self.manager.resident[c]
                 for c in sorted(self.manager.resident)]
        return build_draw_list(camera, cells,
                               placements=self.manager.placements(self.view),
                               view=self.view), cells

    def frame(self, **kwargs):
        draw_list, cells = self.draw_list(**kwargs)
        return self.backend.render(draw_list,
                                   {c.cell_id: c for c in cells},
                                   self.settings())


class TestDrawCallsWithNoGpu(BackendFixture):
    """RENDER_SCOPE §5's main mechanism. None of this needs a driver."""

    def setUp(self):
        self.ctx = RecordingContext()
        self.build(self.ctx)

    def test_static_geometry_is_uploaded_once_per_residency(self):
        """The property the whole `upload_cell` interface exists for.

        Asserted as "**no buffer is created after the first frame**" rather
        than "no buffer is created while drawing", because one *is*: impostors
        stream through a single reused buffer, allocated on the frame it is
        first needed and orphan-and-written thereafter. The first version of
        this test demanded zero and was right to fail - the backend was making
        a fresh buffer and VAO every frame.
        """
        after_load = self.ctx.count("buffer")
        self.assertGreater(after_load, 0, "nothing was uploaded")

        self.frame()
        settled = self.ctx.count("buffer")
        self.assertLessEqual(settled - after_load, 1,
                             "the first frame allocated more than the one "
                             "streaming buffer")

        for _frame in range(10):
            self.frame()
        self.assertEqual(
            self.ctx.count("buffer"), settled,
            "drawing created buffers - static geometry is being re-uploaded "
            "per frame, which is what this interface exists to prevent")

    def test_impostors_stream_through_one_reused_buffer(self):
        """Orphan-and-write, so the driver can hand back storage without
        waiting for the last frame to finish reading the old contents."""
        for _frame in range(5):
            self.frame()
        self.assertGreaterEqual(self.ctx.count("buffer.orphan"), 3)
        self.assertGreaterEqual(self.ctx.count("buffer.write"), 4)

    def test_every_draw_list_item_produces_a_draw(self):
        draw_list, cells = self.draw_list()
        before = self.ctx.count("render")
        self.backend.render(draw_list, {c.cell_id: c for c in cells},
                            self.settings())
        self.assertGreaterEqual(self.ctx.count("render") - before, 1)
        self.assertEqual(self.backend.missing_uploads(), [],
                         "a cell was drawn that had no buffers - it would be a "
                         "hole in the picture and nothing else")

    def test_a_frame_clears_and_enables_depth(self):
        self.frame()
        self.assertGreaterEqual(self.ctx.count("clear"), 1)
        self.assertIn("DEPTH_TEST",
                      [c.target for c in self.ctx.of("enable")])

    def test_unloading_releases_the_buffers(self):
        live_before = len(self.ctx.live_resources())
        self.manager.unload(FIELD)
        self.assertLess(len(self.ctx.live_resources()), live_before,
                        "GPU memory nobody can reclaim")

    def test_damage_re_uploads_the_structure_not_the_cell(self):
        self.frame()
        buffers_before = self.ctx.count("buffer")
        self.manager.damage_structure(self.bridge.frame(), VILLAGE,
                                      GATEHOUSE, [0, 1])
        made = self.ctx.count("buffer") - buffers_before
        self.assertGreaterEqual(made, 1, "the wall came down and nothing moved")
        self.assertLessEqual(made, 1,
                             "a damaged wall re-uploaded more than the one "
                             "structure it touched")

    def test_the_cell_placement_is_sent_once_per_cell(self):
        """Not once per draw. The draw list is already grouped by cell, and a
        redundant uniform per triangle batch is how a GPU path ends up slower
        than it looks."""
        draw_list, cells = self.draw_list()
        cell_ids = {i.cell_id for i in draw_list.items}
        before = len([c for c in self.ctx.of("uniform.write")])
        self.backend.render(draw_list, {c.cell_id: c for c in cells},
                            self.settings())
        writes = len([c for c in self.ctx.of("uniform.write")]) - before
        # one mvp + one model per cell drawn (+ one identity for impostors)
        self.assertLessEqual(writes, len(cell_ids) + 2)

    def test_impostors_do_not_touch_a_residency_buffer(self):
        """Entities, props and items move; their geometry is streamed."""
        self.frame()
        for name in self.backend.cells:
            self.assertIn(name, self.manager.resident)


class TestTheMathsIsRight(unittest.TestCase):
    """Matrices, checked against the geometry they are supposed to reproduce."""

    def test_a_model_matrix_matches_transform_apply(self):
        placement = Transform(position=(3.0, -2.0, 128.0))
        m = _model_matrix(placement)
        point = (1.0, 2.0, 3.0)
        moved = tuple(
            sum(m[r][c] * (point + (1.0,))[c] for c in range(4))
            for r in range(3))
        for got, want in zip(moved, placement.apply(point)):
            self.assertAlmostEqual(got, want, places=6)

    def test_no_placement_is_the_identity(self):
        self.assertEqual(_model_matrix(None), _identity())

    def test_the_view_projection_puts_a_point_ahead_on_screen(self):
        camera = Camera.looking_at((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
        m = _view_projection(camera)
        clip = [sum(m[r][c] * v for c, v in enumerate((0.0, 0.0, 10.0, 1.0)))
                for r in range(4)]
        self.assertGreater(clip[3], 0.0, "a point ahead must have w > 0")
        ndc = [clip[i] / clip[3] for i in range(3)]
        self.assertAlmostEqual(ndc[0], 0.0, places=5)
        self.assertAlmostEqual(ndc[1], 0.0, places=5)

    def test_a_point_behind_the_camera_has_negative_w(self):
        camera = Camera.looking_at((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
        m = _view_projection(camera)
        w = sum(m[3][c] * v for c, v in enumerate((0.0, 0.0, -10.0, 1.0)))
        self.assertLess(w, 0.0)


class TestTheProbeDoesNotEatALiveContext(unittest.TestCase):
    """A real bug, found on real hardware, that no mock would have shown.

    `gl_probe` creates a standalone context and releases it. Doing that while a
    backend is rendering tore the live context down: a framebuffer that had
    just produced a correct frame came back **entirely black** on the next
    read, because `render_resident` -> `select_backend` -> `probe` ran in
    between.
    """

    def setUp(self):
        forget_gl_probe()
        self.addCleanup(forget_gl_probe)

    def test_the_probe_is_memoised(self):
        first = gl_probe()
        self.assertIs(gl_probe(), first,
                      "a second probe would build a second context")

    def test_a_live_context_primes_it_so_no_probe_ever_runs(self):
        """Memoising alone was not enough: in a fresh process the first probe
        is still a *first* call. A context that exists already knows its own
        renderer, so it records it."""
        prime_gl_probe("PrimedRenderer/1.0", "context via test")
        self.assertEqual(gl_probe(), ("PrimedRenderer/1.0", "context via test"))

    def test_priming_does_not_overwrite_a_real_answer(self):
        real = gl_probe()
        prime_gl_probe("SomethingElse", "later")
        self.assertEqual(gl_probe(), real)


@unittest.skipUnless(has_gpu(), "no OpenGL on this machine")
class TestAgainstRealHardware(BackendFixture):
    """Everything interesting about this backend that broke, broke here."""

    WIDTH, HEIGHT = 64, 36

    def setUp(self):
        self.build(None)
        self.addCleanup(self.backend.release)

    def test_it_draws_something_other_than_the_clear_colour(self):
        pixels = self.frame().read_pixels()
        self.assertEqual(len(pixels), self.WIDTH * self.HEIGHT * 3)
        background = tuple(self.settings().background)
        distinct = {tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)}
        self.assertGreater(len(distinct), 1,
                           "the whole frame is one colour - nothing drew")
        self.assertIn(background, distinct, "the sky should be the background")

    def test_the_sky_is_at_the_top(self):
        """`read_pixels` returns top-left origin. OpenGL's framebuffer origin
        is bottom-left, so the first screenshot came out upside down - the
        geometry was right and the convention was not."""
        pixels = self.frame().read_pixels()

        def row_colour(y):
            i = (y * self.WIDTH + self.WIDTH // 2) * 3
            return tuple(pixels[i:i + 3])

        background = tuple(self.settings().background)
        self.assertEqual(row_colour(1), background,
                         "the top row is not sky - the image is flipped")
        self.assertNotEqual(row_colour(self.HEIGHT - 2), background,
                            "the bottom row is sky - the image is flipped")

    def test_a_frame_survives_something_probing_for_a_backend(self):
        """The regression test for the black frame."""
        before = self.frame().read_pixels()
        from lobster.render import select_backend
        select_backend()
        after = self.frame().read_pixels()
        self.assertEqual(before, after)

    def test_distant_ground_is_fogged_and_near_ground_is_not(self):
        """LOBSTER_SCOPE §1: distance fog instead of LOD popping."""
        frame = self.frame(eye=(64.0, 12.0, 10.0), target=(64.0, 0.0, 150.0))
        pixels = frame.read_pixels()

        def at(x, y):
            i = (y * self.WIDTH + x) * 3
            return tuple(pixels[i:i + 3])

        fog = tuple(self.settings().fog_colour)
        near = at(self.WIDTH // 2, self.HEIGHT - 3)
        far = at(self.WIDTH // 2, self.HEIGHT // 2 + 1)
        near_gap = sum(abs(near[i] - fog[i]) for i in range(3))
        far_gap = sum(abs(far[i] - fog[i]) for i in range(3))
        self.assertLess(far_gap, near_gap,
                        "distant ground is no closer to the fog colour than "
                        "near ground - fog is not being applied")

    def test_releasing_frees_everything(self):
        self.frame()
        self.backend.release()
        self.assertEqual(self.backend.cells, {})


if __name__ == "__main__":
    unittest.main()
