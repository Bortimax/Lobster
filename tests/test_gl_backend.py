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
        # Two `mvp` writes, one per program: the static path and the instanced
        # one are separate programs and each needs the view-projection. Then
        # one `model` per cell drawn, plus one identity for the impostors.
        budget = 2 + len(cell_ids) + 1
        self.assertLessEqual(writes, budget,
                             "{0} uniform writes for {1} cells - a redundant "
                             "one per triangle batch is how a GPU path ends up "
                             "slower than it looks".format(writes,
                                                           len(cell_ids)))

    def test_impostors_do_not_touch_a_residency_buffer(self):
        """Entities, props and items move; their geometry is streamed."""
        self.frame()
        for name in self.backend.cells:
            self.assertIn(name, self.manager.resident)


class TestSharedModelBuffers(unittest.TestCase):
    """One buffer per model, however many cells place it (ASSET_SCOPE §2).

    Against `RecordingContext`, because "how many buffers were created" is a
    draw-call assertion and true regardless of driver - the split RENDER_SCOPE
    §5 exists for.
    """

    def setUp(self):
        from tests.fixtures import primitive_library
        self.ctx = RecordingContext()
        self.backend = ModernGLBackend(self.ctx, width=8, height=8)
        self.library = primitive_library()
        self.before = len([c for c in self.ctx.calls if c.what == "buffer"])

    def buffers(self):
        return [c for c in self.ctx.calls if c.what == "buffer"][self.before:]

    def test_uploading_the_same_model_twice_makes_one_buffer(self):
        mesh = self.library.model("model-crate")
        self.backend.upload_model(mesh)
        self.backend.upload_model(mesh)
        self.assertEqual(len(self.buffers()), 1)
        self.assertEqual(sorted(self.backend.models), ["model-crate"])

    def test_the_buffer_is_the_model_s_own_vertices(self):
        mesh = self.library.model("model-barrel")
        self.backend.upload_model(mesh)
        _buf, _vao, count = self.backend.models["model-barrel"]
        self.assertEqual(count, mesh.vertex_count())
        self.assertEqual(self.buffers()[0].detail.get("nbytes"), mesh.nbytes())

    def test_releasing_frees_the_buffer_and_forgets_it(self):
        self.backend.upload_model(self.library.model("model-crate"))
        self.backend.release_model("model-crate")
        self.assertEqual(self.backend.models, {})
        self.assertTrue([c for c in self.ctx.calls if c.what == "release"])

    def test_releasing_one_that_was_never_uploaded_is_not_an_error(self):
        self.backend.release_model("model-nothing")

    def test_an_empty_model_is_not_uploaded(self):
        """A zero-byte buffer is a GL error on some drivers and a model that
        draws nothing on the rest. The build refuses to write one
        (`model_meshes_to_nothing`); this is the belt to that brace."""
        from lobster.model_library import ModelMesh
        self.backend.upload_model(ModelMesh(model_ref="empty", kind="voxel"))
        self.assertEqual(self.backend.models, {})
        self.assertEqual(self.buffers(), [])

    def test_teardown_frees_models_as_well_as_cells(self):
        """`release()` walks `self.cells`, and models are deliberately not in
        there - so without their own loop every one would leak."""
        self.backend.upload_model(self.library.model("model-crate"))
        self.backend.upload_model(self.library.model("model-sign"))
        self.backend.release()
        self.assertEqual(self.backend.models, {})


class InstancingFixture(unittest.TestCase):
    """A cell full of props, a recording context, and models uploaded.

    No GPU: "how many draws for fifty barrels" is a draw-call assertion and true
    regardless of driver, which is the split RENDER_SCOPE §5 exists for.
    """

    WIDTH, HEIGHT = 64, 36

    def build(self, props, *, models=("model-barrel", "model-crate")):
        from lobster.budgets import Budget
        from lobster.cell import ResidentCell
        from tests.fixtures import plain_bundle, primitive_library
        self.library = primitive_library()
        self.ctx = RecordingContext()
        self.backend = ModernGLBackend(self.ctx, width=self.WIDTH,
                                       height=self.HEIGHT)
        self.cells = {}
        for cell_id, cell_props in props.items():
            cell = ResidentCell(plain_bundle(cell_id, props=cell_props),
                                Budget.declared(cell_id, {}))
            self.cells[cell_id] = cell
            self.backend.upload_cell(cell)
        for ref in models:
            self.backend.upload_model(self.library.model(ref))
        self.settings = RenderSettings(width=self.WIDTH, height=self.HEIGHT)
        self.camera = Camera.looking_at(
            (6.0, 2.2, 4.0), (6.0, 0.5, 11.0)).with_aspect(
                self.settings.aspect())

    def prop(self, prop_id, model_ref, position=(6.0, 0.0, 10.0)):
        from lobster.bundle import PropPlacement
        return PropPlacement(prop_id=prop_id, model_ref=model_ref,
                             transform=Transform(position=position))

    def frame(self, *, library=None, placements=None):
        from lobster.visibility import build_draw_list
        draw_list = build_draw_list(
            self.camera, [self.cells[c] for c in sorted(self.cells)],
            library=library or self.library, placements=placements)
        return self.backend.render(draw_list, dict(self.cells), self.settings,
                                   library=library or self.library)

    def instanced_draws(self, since=0):
        return [c for c in self.ctx.of("render")
                if (c.detail.get("instances") or -1) > 0][since:]

    def dynamic_buffers(self):
        return [c for c in self.ctx.of("buffer") if c.detail.get("dynamic")]


class TestInstancing(InstancingFixture):
    """Fifty barrels is one mesh and fifty transforms (ASSET_SCOPE §4)."""

    def test_fifty_barrels_are_one_draw(self):
        self.build({"cell-a": [self.prop("b%d" % i, "model-barrel",
                                         (3.0 + i * 0.2, 0.0, 10.0))
                               for i in range(50)]})
        before = len(self.instanced_draws())
        self.frame()
        draws = self.instanced_draws(before)
        self.assertEqual(len(draws), 1, "one draw per model, not per barrel")
        self.assertEqual(draws[0].detail["instances"], 50)
        self.assertEqual(draws[0].detail["vertices"],
                         self.library.model("model-barrel").vertex_count())

    def test_no_buffer_is_made_per_placement(self):
        """The property the scope named. A buffer per barrel is the thing
        instancing exists to remove."""
        self.build({"cell-a": [self.prop("b%d" % i, "model-barrel",
                                         (3.0 + i * 0.2, 0.0, 10.0))
                               for i in range(50)]})
        before = len(self.ctx.of("buffer"))
        self.frame()
        made = len(self.ctx.of("buffer")) - before
        self.assertLessEqual(made, 1,
                             "{0} buffers for 50 placements".format(made))

    def test_two_models_are_two_draws(self):
        self.build({"cell-a": [self.prop("b", "model-barrel"),
                               self.prop("c", "model-crate", (7.0, 0.0, 10.0))]})
        before = len(self.instanced_draws())
        self.frame()
        draws = self.instanced_draws(before)
        self.assertEqual(len(draws), 2)
        self.assertEqual(sorted(d.detail["vertices"] for d in draws),
                         sorted([self.library.model("model-barrel").vertex_count(),
                                 self.library.model("model-crate").vertex_count()]))

    def test_one_model_across_two_cells_is_still_one_draw(self):
        """ASSET_SCOPE §7 step 5 asked for one draw per model *per cell*. The
        instance matrix carries the cell placement composed in, so the cell
        stopped being a grouping key - strictly fewer draws, same property
        (D50)."""
        self.build({"cell-a": [self.prop("a", "model-barrel", (5.0, 0.0, 10.0))],
                    "cell-b": [self.prop("b", "model-barrel", (7.0, 0.0, 10.0))]})
        before = len(self.instanced_draws())
        self.frame(placements={"cell-b": Transform(position=(0.0, 0.0, 0.0))})
        draws = self.instanced_draws(before)
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0].detail["instances"], 2)

    def test_ten_frames_allocate_nothing(self):
        """Orphan-and-write, not create-and-destroy - the idiom
        `_draw_impostors` already records, applied to instance data."""
        self.build({"cell-a": [self.prop("b%d" % i, "model-barrel",
                                         (3.0 + i * 0.4, 0.0, 10.0))
                               for i in range(6)]})
        self.frame()
        before = len(self.ctx.of("buffer"))
        for _ in range(10):
            self.frame()
        self.assertEqual(len(self.ctx.of("buffer")), before,
                         "a frame allocated a buffer")

    def test_growth_replaces_the_buffer_rather_than_resizing_every_frame(self):
        self.build({"cell-a": [self.prop("b%d" % i, "model-barrel",
                                         (3.0 + i * 0.2, 0.0, 10.0))
                               for i in range(40)]})
        self.frame()
        first = len(self.dynamic_buffers())
        self.frame()
        self.assertEqual(len(self.dynamic_buffers()), first,
                         "the instance buffer was reallocated on a frame that "
                         "needed no more room")

    def test_a_prop_with_no_model_is_still_an_impostor(self):
        self.build({"cell-a": [self.prop("n", "")]})
        before = len(self.instanced_draws())
        self.frame()
        self.assertEqual(self.instanced_draws(before), [])

    def test_a_model_residency_never_uploaded_falls_back_and_is_named(self):
        """A bundle and a library built apart. The picture gets an impostor and
        `missing_models()` says which, because silence names nobody."""
        self.build({"cell-a": [self.prop("g", "model-sign")]},
                   models=("model-barrel",))
        before = len(self.instanced_draws())
        self.frame()
        self.assertEqual(self.instanced_draws(before), [])
        self.assertEqual(self.backend.missing_models(), ["model-sign"])

    def test_releasing_a_model_takes_its_instance_vao_with_it(self):
        """The VAO holds a reference to the model's vertex buffer. Leaving it
        would mean the next upload of the same ref drew the last residency's
        geometry."""
        self.build({"cell-a": [self.prop("b", "model-barrel")]})
        self.frame()
        self.assertIn("model-barrel", self.backend._instances)
        self.backend.release_model("model-barrel")
        self.assertNotIn("model-barrel", self.backend._instances)
        self.assertNotIn("model-barrel", self.backend.models)

    def test_teardown_frees_the_instance_buffers(self):
        """Through `release_model`, which owns the pair. An instance entry
        cannot outlive its model, so there is no second loop to write."""
        self.build({"cell-a": [self.prop("b", "model-barrel")]})
        self.frame()
        self.assertTrue(self.backend._instances)
        self.backend.release()
        self.assertEqual(self.backend._instances, {})
        self.assertEqual(self.backend.models, {})
        self.assertEqual(self.ctx.live_resources(), [],
                         "the context still holds resources after teardown")

    def test_both_programs_get_the_frame_uniforms(self):
        """The instanced program is a second program, so it needs its own
        `mvp`. Without one every instanced draw collapses to the origin - and
        no draw-call count can see that."""
        self.build({"cell-a": [self.prop("b", "model-barrel")]})
        before = len(self.ctx.of("uniform.write"))
        self.frame()
        writes = self.ctx.of("uniform.write")[before:]
        mvp_targets = {c.target for c in writes if c.detail.get("key") == "mvp"}
        self.assertEqual(len(mvp_targets), 2,
                         "only {0} program(s) were given the view-projection: "
                         "{1}".format(len(mvp_targets), sorted(mvp_targets)))


class TestTheInstancePayload(unittest.TestCase):
    """What each instance carries, unpacked.

    A unit test on `_instance_bytes` rather than a read-back of the buffer: the
    recorder keeps sizes and not payloads, and a wrong matrix should be a failed
    assertion here rather than a picture nobody looked at.
    """

    def item(self, position, cell_placement=None, degrees=0.0):
        import math
        from lobster.visibility import DrawItem, PROP
        half = math.radians(degrees / 2.0)
        return DrawItem(
            kind=PROP, cell_id="cell-a", item_id="b",
            cell_placement=cell_placement or Transform(),
            center=position, radius=1.0, distance=1.0,
            model_ref="model-barrel",
            transform=Transform(position=position,
                                rotation=(0.0, math.sin(half), 0.0,
                                          math.cos(half))))

    class Lit:
        def __init__(self, level):
            self.level = level

        def ambient_at(self, point):
            self.asked = point
            return self.level

    def unpack(self, item, cell):
        import struct
        from lobster.render.gl_backend import (INSTANCE_STRIDE,
                                               _instance_bytes)
        data = _instance_bytes(item, cell)
        self.assertEqual(len(data), INSTANCE_STRIDE)
        return struct.unpack("17f", data)

    def test_the_matrix_is_the_world_placement(self):
        """Column-major, so the translation is the fourth column - and it is
        the prop's position composed with its cell's, not either alone."""
        row = self.unpack(
            self.item((5.0, 0.0, 10.0),
                      cell_placement=Transform(position=(3.0, 1.0, 0.0))),
            self.Lit(1.0))
        self.assertAlmostEqual(row[12], 8.0, places=5)
        self.assertAlmostEqual(row[13], 1.0, places=5)
        self.assertAlmostEqual(row[14], 10.0, places=5)

    def test_rotation_is_in_the_matrix_and_the_position_is_not_moved_by_it(self):
        straight = self.unpack(self.item((5.0, 0.0, 10.0)), self.Lit(1.0))
        turned = self.unpack(self.item((5.0, 0.0, 10.0), degrees=45.0),
                             self.Lit(1.0))
        self.assertEqual(straight[12:15], turned[12:15],
                         "turning the crate moved it")
        self.assertNotEqual(straight[0:12], turned[0:12],
                            "the rotation is not in the matrix at all")

    def test_the_light_is_sampled_where_the_thing_stands(self):
        """A library tint is unlit by construction (D47), so the bake arrives
        per instance - sampled at the placement, which is what Scope §3 already
        asks of structures."""
        cell = self.Lit(0.25)
        row = self.unpack(
            self.item((5.0, 0.0, 10.0),
                      cell_placement=Transform(position=(3.0, 0.0, 0.0))),
            cell)
        self.assertAlmostEqual(row[16], 0.25, places=5)
        self.assertEqual(cell.asked, (8.0, 0.0, 10.0),
                         "the light was sampled somewhere other than where the "
                         "instance actually stands")

    def test_a_cell_with_no_lightmap_is_fully_lit_rather_than_black(self):
        class Bare:
            pass

        row = self.unpack(self.item((5.0, 0.0, 10.0)), Bare())
        self.assertAlmostEqual(row[16], 1.0, places=5)


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

    def test_instanced_props_actually_reach_the_framebuffer(self):
        """On a driver, not against a recorder.

        There is deliberately no pixel comparison with the software path (§2),
        so this asserts the only thing that is true regardless: a frame with
        instanced geometry is not the frame without it, and nothing fell back
        to an impostor on the way.
        """
        from lobster.budgets import Budget
        from lobster.bundle import PropPlacement
        from lobster.cell import ResidentCell
        from lobster.visibility import build_draw_list
        from tests.fixtures import plain_bundle, primitive_library

        library = primitive_library()
        props = [PropPlacement(prop_id="b%d" % i, model_ref="model-barrel",
                               transform=Transform(
                                   position=(4.0 + i * 0.8, 0.0, 10.0)))
                 for i in range(6)]
        cell = ResidentCell(plain_bundle("cell-props", props=props),
                            Budget.declared("cell-props", {}))
        camera = Camera.looking_at((6.0, 2.0, 5.0),
                                   (6.0, 0.5, 11.0)).with_aspect(
                                       self.settings().aspect())

        def shot(with_models):
            backend = ModernGLBackend(width=self.WIDTH, height=self.HEIGHT)
            self.addCleanup(backend.release)
            backend.upload_cell(cell)
            if with_models:
                for ref in library.model_refs():
                    backend.upload_model(library.model(ref))
            draw_list = build_draw_list(
                camera, [cell], library=library if with_models else None)
            frame = backend.render(draw_list, {"cell-props": cell},
                                   self.settings(),
                                   library=library if with_models else None)
            return bytes(frame.read_pixels()), backend

        # Two backends and not two frames on one: `missing_models()` is a
        # cumulative record, like `missing_uploads`, so a deliberate frame with
        # nothing uploaded would stay in it and make the assertion below false
        # for the right reason at the wrong time.
        bare, _ = shot(False)
        drawn, backend = shot(True)

        self.assertNotEqual(bare, drawn,
                            "uploading the models changed nothing on screen")
        self.assertEqual(backend.missing_models(), [],
                         "something fell back to an impostor")


if __name__ == "__main__":
    unittest.main()


class TestACharacterReachesTheGpuAsGeometry(InstancingFixture):
    """The GL path routed entities to the impostor branch unconditionally, so
    a dressed character uploaded its models and then drew a grey box over
    them. The llvmpipe job could not see it: nothing failed, the picture was
    just wrong, and no GL test mentioned entities at all (D57).
    """

    HELM = "model-crate"

    def dress(self, cell_id, entity_id, position, bones=("head",)):
        from lobster.skeleton import Skeleton, Wardrobe, humanoid_region_set
        from lobster.tiers import ACTIVE
        skeleton = Skeleton(
            entity_id, humanoid_region_set(),
            root=Transform(position=position),
            wardrobe=Wardrobe("guard", {b: self.HELM for b in bones}))
        self.cells[cell_id].place(entity_id, position, ACTIVE,
                                  skeleton=skeleton)
        return skeleton

    def test_a_worn_model_is_an_instanced_draw_not_an_impostor(self):
        self.build({"cell-a": []}, models=(self.HELM,))
        self.dress("cell-a", "npc-ada", (6.0, 0.0, 10.0))
        before = len(self.instanced_draws())
        self.frame()
        draws = self.instanced_draws(before)
        self.assertEqual(len(draws), 1, "the helm did not reach the GPU")
        self.assertEqual(draws[0].detail["vertices"],
                         self.library.model(self.HELM).vertex_count())

    def test_a_guard_and_a_crate_in_one_helm_are_one_draw(self):
        """Worn models take the path props take, so they group with them."""
        self.build({"cell-a": [self.prop("c", self.HELM, (5.0, 0.0, 10.0))]},
                   models=(self.HELM,))
        self.dress("cell-a", "npc-ada", (7.0, 0.0, 10.0))
        before = len(self.instanced_draws())
        self.frame()
        draws = self.instanced_draws(before)
        self.assertEqual(len(draws), 1, "one model, one draw")
        self.assertEqual(draws[0].detail["instances"], 2,
                         "the crate and the helm should be two instances of "
                         "one mesh")

    def test_six_guards_are_one_draw_and_six_instances(self):
        self.build({"cell-a": []}, models=(self.HELM,))
        for i in range(6):
            self.dress("cell-a", "npc-%d" % i, (3.0 + i * 0.8, 0.0, 10.0))
        before = len(self.instanced_draws())
        self.frame()
        draws = self.instanced_draws(before)
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0].detail["instances"], 6)

    def test_an_undressed_entity_is_still_an_impostor(self):
        """The fallback did not move."""
        from lobster.geometry import Transform as _T
        from lobster.skeleton import Skeleton, humanoid_region_set
        from lobster.tiers import ACTIVE
        self.build({"cell-a": []}, models=(self.HELM,))
        self.cells["cell-a"].place(
            "npc-bare", (6.0, 0.0, 10.0), ACTIVE,
            skeleton=Skeleton("npc-bare", humanoid_region_set(),
                              root=_T(position=(6.0, 0.0, 10.0))))
        before = len(self.instanced_draws())
        self.frame()
        self.assertEqual(len(self.instanced_draws(before)), 0,
                         "an undressed entity produced an instanced draw")
        # The half that is not visible on screen: an entity with no wardrobe
        # has an empty `model_ref`, and an empty ref is a documented absence.
        # Letting it reach the library lookup reports `""` as a model residency
        # failed to upload - the same absence-into-fault this project refuses
        # for a prop with no model.
        self.assertNotIn("", self.backend.missing_models(),
                         "an entity with no wardrobe was reported as a "
                         "missing model")

    def test_a_worn_model_residency_never_uploaded_is_counted(self):
        """Same fallback an absent barrel gets - named, not raised."""
        self.build({"cell-a": []}, models=())
        self.dress("cell-a", "npc-ada", (6.0, 0.0, 10.0))
        before = len(self.instanced_draws())
        self.frame()
        self.assertEqual(len(self.instanced_draws(before)), 0)
        self.assertIn(self.HELM, self.backend.missing_models())
