"""The recording context, tested as an instrument (RENDER_SCOPE §5, step 2).

Built before the GPU backend, for the reason D28 gives about building the
differential harness before the kernels: a verification tool written afterwards
is shaped by the thing it is meant to check, and it cannot become a third
named-but-unwritten item if it exists first.

Two halves here, and the second is the one that matters:

1. the recorder records - straightforward;
2. **the recorder resembles ModernGL**, asserted against the installed library
   rather than against my memory of its API. A fake that has drifted from the
   real thing is worse than no fake, because it passes.
"""

from __future__ import annotations

import unittest

from lobster.render.recording import (Call, RecordingContext, RecordingError,
                                      _nbytes)

try:
    import moderngl
except Exception:                                    # pragma: no cover
    moderngl = None


class TestItRecords(unittest.TestCase):

    def setUp(self):
        self.ctx = RecordingContext()

    def test_a_buffer_records_its_size(self):
        buf = self.ctx.buffer(data=b"0123456789")
        self.assertEqual(self.ctx.count("buffer"), 1)
        self.assertEqual(self.ctx.of("buffer")[0].detail["nbytes"], 10)
        self.assertEqual(buf.size, 10)

    def test_a_reserved_buffer_records_its_reservation(self):
        self.ctx.buffer(reserve=4096, dynamic=True)
        detail = self.ctx.of("buffer")[0].detail
        self.assertEqual(detail["nbytes"], 4096)
        self.assertTrue(detail["dynamic"])

    def test_draws_are_recorded_with_their_vertex_count(self):
        program = self.ctx.program(vertex_shader="v", fragment_shader="f")
        vao = self.ctx.vertex_array(program, [(self.ctx.buffer(b"xy"), "2f", "in")])
        vao.render(vertices=36)
        self.assertEqual(self.ctx.of("render")[0].detail["vertices"], 36)

    def test_uniforms_are_recorded(self):
        program = self.ctx.program(vertex_shader="v", fragment_shader="f")
        program["mvp"].value = (1, 0, 0)
        self.assertEqual(self.ctx.of("uniform")[0].detail["key"], "mvp")
        self.assertEqual(program["mvp"].value, (1, 0, 0))

    def test_live_resources_is_the_leak_check(self):
        buf = self.ctx.buffer(b"abcd")
        self.ctx.buffer(b"efgh")
        self.assertEqual(len(self.ctx.live_resources()), 2)
        buf.release()
        self.assertEqual(len(self.ctx.live_resources()), 1)

    def test_a_double_release_is_refused(self):
        """Drivers tolerate this to varying degrees. Lobster should not rely on
        that, and a double release is a bookkeeping bug either way."""
        buf = self.ctx.buffer(b"abcd")
        buf.release()
        with self.assertRaises(RecordingError):
            buf.release()

    def test_drawing_after_release_is_refused(self):
        program = self.ctx.program(vertex_shader="v", fragment_shader="f")
        vao = self.ctx.vertex_array(program, [])
        vao.release()
        with self.assertRaises(RecordingError):
            vao.render(vertices=3)

    def test_an_unknown_call_raises_rather_than_being_absorbed(self):
        """A mock that quietly accepts anything stops telling the truth."""
        with self.assertRaises(AttributeError):
            self.ctx.compute_shader("...")

    def test_a_call_renders_readably(self):
        """These end up in assertion messages, so they have to be legible."""
        self.assertEqual(str(Call("render", "vao-1", {"vertices": 12})),
                         "render(vao-1) vertices=12")


class TestTheRecorderCanFail(unittest.TestCase):
    """The anti-vacuity half. Each assertion the GPU backend will rely on gets
    a violation injected here, so a recorder that cannot detect one is caught
    before anything is written against it."""

    def setUp(self):
        self.ctx = RecordingContext()

    def upload(self):
        return self.ctx.buffer(b"vertexdata" * 8)

    def test_it_notices_a_buffer_that_was_never_released(self):
        self.upload()
        self.assertTrue(self.ctx.live_resources(),
                        "a leak the assertion must be able to see")

    def test_it_notices_an_upload_happening_every_frame(self):
        """The property the whole `upload_cell` interface exists for. Three
        frames must not mean three uploads."""
        for _frame in range(3):
            self.upload()
        self.assertEqual(self.ctx.count("buffer"), 3,
                         "the recorder must be able to count this in order to "
                         "assert it does not happen")

    def test_it_notices_a_draw_that_never_happened(self):
        program = self.ctx.program(vertex_shader="v", fragment_shader="f")
        self.ctx.vertex_array(program, [])
        self.assertEqual(self.ctx.count("render"), 0)

    def test_the_summary_is_a_whole_frame_fingerprint(self):
        program = self.ctx.program(vertex_shader="v", fragment_shader="f")
        vao = self.ctx.vertex_array(program, [self.upload()])
        vao.render(vertices=6)
        vao.render(vertices=6)
        summary = self.ctx.summary()
        self.assertEqual(summary["render"], 2)
        self.assertEqual(summary["buffer"], 1)


@unittest.skipUnless(moderngl is not None, "moderngl is not installed here")
class TestItResemblesModernGL(unittest.TestCase):
    """The check that keeps this honest.

    A fake API is worth its resemblance to the real one, and the usual way that
    resemblance dies is quietly: the library moves, the fake does not, and the
    tests keep passing against a contract nobody implements any more.

    So the resemblance is asserted against the installed ModernGL. This is
    skipped where ModernGL is absent - it cannot run - but it runs on every job
    of the accelerated CI matrix.
    """

    def test_every_context_method_exists_on_moderngl(self):
        ctx = RecordingContext()
        mine = [n for n in dir(ctx)
                if not n.startswith("_")
                and callable(getattr(ctx, n))
                and n not in ("record", "of", "count", "live_resources",
                              "summary")]
        for name in mine:
            self.assertTrue(
                hasattr(moderngl.Context, name),
                "RecordingContext.{0} does not exist on moderngl.Context - "
                "the fake has drifted".format(name))

    def test_context_methods_accept_the_arguments_we_pass(self):
        """Names matching is not enough; the call has to be one ModernGL would
        accept."""
        import inspect
        checks = [
            ("buffer", {"data": b"x", "reserve": 0, "dynamic": False}),
            ("texture", {"size": (4, 4), "components": 3}),
            ("framebuffer", {"color_attachments": (), "depth_attachment": None}),
        ]
        for name, kwargs in checks:
            signature = inspect.signature(getattr(moderngl.Context, name))
            for key in kwargs:
                self.assertIn(
                    key, signature.parameters,
                    "moderngl.Context.{0} takes no {1!r} - the recorder "
                    "accepts a call the real thing would reject".format(
                        name, key))

    def test_resource_methods_exist_on_their_moderngl_counterparts(self):
        pairs = [("RecordingBuffer", moderngl.Buffer,
                  ("write", "release", "orphan")),
                 ("RecordingVertexArray", moderngl.VertexArray,
                  ("render", "release")),
                 ("RecordingTexture", moderngl.Texture,
                  ("write", "read", "use", "release")),
                 ("RecordingFramebuffer", moderngl.Framebuffer,
                  ("use", "clear", "read", "release"))]
        for label, real, methods in pairs:
            for name in methods:
                self.assertTrue(hasattr(real, name),
                                "{0}.{1} has no counterpart".format(label, name))

    def test_render_takes_moderngls_own_parameters(self):
        import inspect
        from lobster.render.recording import RecordingVertexArray
        real = inspect.signature(moderngl.VertexArray.render).parameters
        mine = inspect.signature(RecordingVertexArray.render).parameters
        for name in mine:
            if name == "self":
                continue
            self.assertIn(name, real,
                          "render({0}=...) is not something ModernGL "
                          "accepts".format(name))

    def test_the_context_reports_a_renderer_string_like_the_real_one(self):
        """`gl_probe` reads `info['GL_RENDERER']` to pick a tier (D29). A
        recorder that omitted it would break tier selection under test."""
        self.assertIn("GL_RENDERER", RecordingContext().info)


class TestByteCounting(unittest.TestCase):
    """`_nbytes` decides what "uploaded" means in every assertion."""

    def test_it_measures_buffers_arrays_and_bytes_alike(self):
        self.assertEqual(_nbytes(b"abcd"), 4)
        self.assertEqual(_nbytes(bytearray(7)), 7)
        import array
        self.assertEqual(_nbytes(array.array("f", [1.0, 2.0])), 8)

    def test_nothing_is_zero_rather_than_an_error(self):
        self.assertEqual(_nbytes(None), 0)


if __name__ == "__main__":
    unittest.main()
