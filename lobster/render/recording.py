"""A recording stand-in for a ModernGL context (RENDER_SCOPE §5).

This is what replaces the pixel oracle. The project owner deleted pixel
agreement between backends — two rasterisers producing different pixels is not
a defect, and no tolerance separates "different because correct" from
"different because broken". So GPU correctness is established by asserting
**what the backend did**, not what came out:

* one upload per cell residency, and not one per frame;
* every uploaded buffer released when the cell unloads;
* every item in the draw list producing a draw, and culled items producing
  none;
* a damaged structure re-uploading the touched chunk and not the cell.

Those are true regardless of driver, resolution or sun angle, and every one of
them runs with **no GPU present** — including in the pure-Python CI job.

## Why this is not a mock that quietly drifts

A fake API is worth exactly as much as its resemblance to the real one, and the
usual failure is that the real library moves and the fake does not. So the
resemblance is **asserted**: `tests/test_recording.py` walks this module against
the installed `moderngl` and fails if anything here names a method ModernGL does
not have, or takes an argument it would reject.

That check is skipped when ModernGL is absent - it cannot run without it - but
it runs everywhere ModernGL is installed, which now includes the CI matrix.

## What it deliberately does not do

No rendering, no maths, no pixels. A recorder that tried to be a software
rasteriser would be a third implementation to keep correct, and the one we have
is already the reference for that job.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple


class RecordingError(Exception):
    pass


@dataclass
class Call:
    """One thing the backend asked the context to do."""

    what: str
    target: Optional[str] = None
    detail: Dict[str, Any] = dc_field(default_factory=dict)

    def __str__(self) -> str:
        bits = "".join(" {0}={1!r}".format(k, v)
                       for k, v in sorted(self.detail.items()))
        return "{0}({1}){2}".format(self.what, self.target or "", bits)


class _Resource:
    """Base for the objects a context hands back. Records its own release."""

    def __init__(self, ctx: "RecordingContext", name: str) -> None:
        self._ctx = ctx
        self.name = name
        self.released = False

    def release(self) -> None:
        if self.released:
            # Real drivers tolerate this to varying degrees; Lobster should not
            # rely on that. A double release is a bookkeeping bug.
            raise RecordingError("{0} released twice".format(self.name))
        self.released = True
        self._ctx.record("release", self.name)


class RecordingBuffer(_Resource):
    def __init__(self, ctx: "RecordingContext", name: str, nbytes: int,
                 dynamic: bool) -> None:
        super().__init__(ctx, name)
        self.size = nbytes
        self.dynamic = dynamic

    def write(self, data: Any, offset: int = 0) -> None:
        self._ctx.record("buffer.write", self.name,
                         nbytes=_nbytes(data), offset=offset)

    def orphan(self, size: int = -1) -> None:
        self._ctx.record("buffer.orphan", self.name, size=size)


class RecordingProgram(_Resource):
    def __init__(self, ctx: "RecordingContext", name: str,
                 uniforms: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(ctx, name)
        self._uniforms: Dict[str, Any] = dict(uniforms or {})

    def __getitem__(self, key: str) -> "RecordingUniform":
        return RecordingUniform(self._ctx, self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._uniforms.get(key, default)


class RecordingUniform:
    def __init__(self, ctx: "RecordingContext", program: RecordingProgram,
                 key: str) -> None:
        self._ctx = ctx
        self._program = program
        self._key = key

    @property
    def value(self) -> Any:
        return self._program.get(self._key)

    @value.setter
    def value(self, v: Any) -> None:
        self._program._uniforms[self._key] = v
        self._ctx.record("uniform", self._program.name, key=self._key)

    def write(self, data: Any) -> None:
        self._ctx.record("uniform.write", self._program.name, key=self._key,
                         nbytes=_nbytes(data))


class RecordingVertexArray(_Resource):
    def __init__(self, ctx: "RecordingContext", name: str,
                 program: RecordingProgram,
                 buffers: Sequence[RecordingBuffer]) -> None:
        super().__init__(ctx, name)
        self.program = program
        self.buffers = list(buffers)

    def render(self, mode: Any = None, vertices: int = -1, first: int = 0,
               instances: int = -1) -> None:
        if self.released:
            raise RecordingError("{0} drew after release".format(self.name))
        self._ctx.record("render", self.name, vertices=vertices,
                         instances=instances)


class RecordingTexture(_Resource):
    def __init__(self, ctx: "RecordingContext", name: str,
                 size: Tuple[int, int], components: int) -> None:
        super().__init__(ctx, name)
        self.size = size
        self.components = components

    def write(self, data: Any, viewport: Any = None) -> None:
        self._ctx.record("texture.write", self.name, nbytes=_nbytes(data))

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        self._ctx.record("texture.read", self.name)
        return bytes(self.size[0] * self.size[1] * self.components)

    def use(self, location: int = 0) -> None:
        self._ctx.record("texture.use", self.name, location=location)


class RecordingFramebuffer(_Resource):
    def __init__(self, ctx: "RecordingContext", name: str,
                 colour: Any = None, depth: Any = None) -> None:
        super().__init__(ctx, name)
        self.color_attachments = tuple(colour or ())
        self.depth_attachment = depth

    def use(self) -> None:
        self._ctx.record("framebuffer.use", self.name)

    def clear(self, *args: Any, **kwargs: Any) -> None:
        self._ctx.record("clear", self.name)

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        self._ctx.record("framebuffer.read", self.name)
        size = getattr(self.color_attachments[0], "size", (1, 1)) \
            if self.color_attachments else (1, 1)
        return bytes(size[0] * size[1] * 3)


class RecordingContext:
    """Stands in for `moderngl.Context` and remembers everything.

    Only the surface a renderer needs. Anything the backend calls that is not
    here raises `AttributeError` immediately, which is the correct outcome: a
    silently-absorbed call is how a mock stops telling the truth.
    """

    #: Enough of the module-level constants to compile against.
    DEPTH_TEST = "DEPTH_TEST"
    CULL_FACE = "CULL_FACE"
    BLEND = "BLEND"
    TRIANGLES = "TRIANGLES"

    def __init__(self, renderer: str = "RecordingContext/software") -> None:
        self.calls: List[Call] = []
        self.info = {"GL_RENDERER": renderer, "GL_VERSION": "3.3.0 recording"}
        self.viewport = (0, 0, 0, 0)
        self.released = False
        self._counter = 0

    # -- bookkeeping ---------------------------------------------------------
    def record(self, what: str, target: Optional[str] = None,
               **detail: Any) -> None:
        self.calls.append(Call(what, target, detail))

    def _name(self, prefix: str) -> str:
        self._counter += 1
        return "{0}-{1}".format(prefix, self._counter)

    # -- the context surface -------------------------------------------------
    def buffer(self, data: Any = None, reserve: int = 0,
               dynamic: bool = False) -> RecordingBuffer:
        name = self._name("buffer")
        nbytes = _nbytes(data) if data is not None else int(reserve)
        self.record("buffer", name, nbytes=nbytes, dynamic=dynamic)
        return RecordingBuffer(self, name, nbytes, dynamic)

    def program(self, vertex_shader: str = "", fragment_shader: str = "",
                **kwargs: Any) -> RecordingProgram:
        name = self._name("program")
        self.record("program", name,
                    vertex_chars=len(vertex_shader),
                    fragment_chars=len(fragment_shader))
        return RecordingProgram(self, name)

    def vertex_array(self, program: RecordingProgram, *args: Any,
                     **kwargs: Any) -> RecordingVertexArray:
        name = self._name("vao")
        buffers = _buffers_in(args) + _buffers_in(tuple(kwargs.values()))
        self.record("vertex_array", name, program=program.name,
                    buffers=len(buffers))
        return RecordingVertexArray(self, name, program, buffers)

    simple_vertex_array = vertex_array

    def texture(self, size: Tuple[int, int], components: int,
                data: Any = None, **kwargs: Any) -> RecordingTexture:
        name = self._name("texture")
        self.record("texture", name, size=tuple(size), components=components)
        return RecordingTexture(self, name, tuple(size), components)

    def depth_texture(self, size: Tuple[int, int],
                      data: Any = None, **kwargs: Any) -> RecordingTexture:
        name = self._name("depth")
        self.record("depth_texture", name, size=tuple(size))
        return RecordingTexture(self, name, tuple(size), 1)

    def framebuffer(self, color_attachments: Any = (),
                    depth_attachment: Any = None) -> RecordingFramebuffer:
        name = self._name("fbo")
        colour = (color_attachments if isinstance(color_attachments, (list, tuple))
                  else (color_attachments,))
        self.record("framebuffer", name, attachments=len(colour))
        return RecordingFramebuffer(self, name, colour, depth_attachment)

    def clear(self, *args: Any, **kwargs: Any) -> None:
        self.record("clear", "context")

    def enable(self, flag: Any) -> None:
        self.record("enable", str(flag))

    def disable(self, flag: Any) -> None:
        self.record("disable", str(flag))

    def release(self) -> None:
        self.released = True
        self.record("release", "context")

    # -- queries the assertions are written against --------------------------
    def of(self, what: str) -> List[Call]:
        return [c for c in self.calls if c.what == what]

    def count(self, what: str) -> int:
        return len(self.of(what))

    def live_resources(self) -> List[str]:
        """Everything created and not released, in creation order.

        This is the leak check. D43 is a fresh reminder that a resource bug
        passes every test that only looks at answers.
        """
        created = [c.target for c in self.calls
                   if c.what in ("buffer", "program", "vertex_array", "texture",
                                 "depth_texture", "fbo", "framebuffer")]
        released = {c.target for c in self.of("release")}
        return [name for name in created if name not in released]

    def summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for call in self.calls:
            out[call.what] = out.get(call.what, 0) + 1
        return out


def _nbytes(data: Any) -> int:
    if data is None:
        return 0
    try:
        return len(memoryview(data).tobytes())
    except TypeError:
        try:
            return len(data)
        except TypeError:
            return 0


def _buffers_in(values: Sequence[Any]) -> List[RecordingBuffer]:
    found: List[RecordingBuffer] = []
    for value in values:
        if isinstance(value, RecordingBuffer):
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(_buffers_in(value))
    return found


# ---------------------------------------------------------------------------
# A backend that records instead of drawing
# ---------------------------------------------------------------------------

class RecordingBackend:
    """A `RenderBackend` that remembers what it was asked to do.

    The companion to `RecordingContext`, one level up: that one stands in for
    the GL library, this one stands in for the backend while there is not yet a
    real GPU backend to bind residency to. It is what step 3's assertions are
    written against, and it stays useful afterwards - a test about *residency*
    should not need a GL context at all.
    """

    name = "recording"

    def __init__(self) -> None:
        self.uploaded: List[str] = []
        self.released: List[str] = []
        self.structure_uploads: List[Tuple[str, str, Tuple[int, ...]]] = []
        #: every `upload_model` and `release_model`, in order. Lists and not
        #: sets: "uploaded once" is a claim about how many times, and a set
        #: cannot tell you that.
        self.model_uploads: List[str] = []
        self.model_releases: List[str] = []
        #: every draw list this backend was handed, in order. What reached the
        #: *culler* is not visible in the pixels - two draw lists that differ
        #: only in a cull radius can render identically - so a test that cares
        #: which radius was used looks here.
        self.draw_lists: List[Any] = []
        #: and the library it was handed with each of them.
        self.libraries: List[Any] = []
        self.frames = 0

    @classmethod
    def available(cls) -> Any:
        from .backend import BackendInfo
        return BackendInfo(name=cls.name, available=True,
                           detail="records calls; draws nothing")

    def upload_cell(self, cell: Any) -> None:
        self.uploaded.append(cell.cell_id)

    def release_cell(self, cell_id: str) -> None:
        self.released.append(cell_id)

    def upload_model(self, mesh: Any) -> None:
        self.model_uploads.append(mesh.model_ref)

    def release_model(self, model_ref: str) -> None:
        self.model_releases.append(model_ref)

    def upload_structure(self, cell: Any, structure_id: str,
                         chunk_indices: Sequence[int]) -> None:
        self.structure_uploads.append(
            (cell.cell_id, structure_id, tuple(chunk_indices)))

    def render(self, draw_list: Any, cells_by_id: Dict[str, Any],
               settings: Any, *, library: Any = None) -> Any:
        self.draw_lists.append(draw_list)
        self.libraries.append(library)
        self.frames += 1
        return None

    # -- queries -------------------------------------------------------------
    def live_cells(self) -> List[str]:
        """Uploaded and not released, in upload order."""
        out: List[str] = []
        released = list(self.released)
        for cell_id in self.uploaded:
            if cell_id in released:
                released.remove(cell_id)
            else:
                out.append(cell_id)
        return out

    def upload_count(self, cell_id: str) -> int:
        return self.uploaded.count(cell_id)

    def live_models(self) -> List[str]:
        """Uploaded and not released. The backend's own opinion, which is the
        one worth comparing a refcount against."""
        out: List[str] = []
        released = list(self.model_releases)
        for model_ref in self.model_uploads:
            if model_ref in released:
                released.remove(model_ref)
            else:
                out.append(model_ref)
        return sorted(out)
