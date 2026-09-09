"""Renderer backends: use the GPU when it is there, fall back when it is not.

The decision this file encodes: **Lobster may leverage the GPU when it is
available.** That resolves the tension DECISIONS.md D6 created - stdlib-only was
chosen for the *shell*, and letting it silently pick the answer to "how do we
draw 3D" was the L1 inversion recorded in D22.

So:

* **Core Lobster keeps no hard graphics dependency.** `pip install lobster` and
  every test still runs on a build machine with no GPU and no display. L7 holds:
  nothing in the shell drags a graphics stack in.
* **A GPU backend is used when it imports.** `select_backend()` prefers it and
  says, in words, why it fell back when it did not.
* **The software backend is the reference**, not the target. It is what CI
  asserts pixels against and what a headless box gets. It is not, and will not
  become, the renderer a player runs.

`available()` is a *class* method on purpose: choosing a backend must not
require constructing one, because constructing a GPU backend is exactly the
thing that fails on a machine without a GPU.

## Three tiers, in preference order (DECISIONS.md D29)

| tier | what it is | what to expect of it |
|---|---|---|
| `moderngl` | OpenGL 3.3+ on a real GPU | the player-facing target: 60 FPS on a 128 m cell with destructible geometry, and the battery-efficient path on a phone |
| `moderngl-llvmpipe` | the same GL code on Mesa's software rasteriser | **fast enough for development and many tests.** Not a player-facing path, and saying so here means nobody benchmarks against it by accident |
| `software` | the pure-Python rasteriser | correct pixels for a screenshot, a debug overlay or a CI oracle. Slow, and viable *everywhere* |

The middle tier is **optional and never vendored**. llvmpipe is an OS package
(Mesa), not something Lobster ships or assumes; when it is absent the chain drops
to pure Python without drama. And the bottom tier stays viable on purpose: some
CI images have Mesa and some do not, so the zero-external-package path must keep
working or CI becomes environment-dependent.

The top two tiers are **the same code** - llvmpipe is a driver, not a renderer -
which is why they share a class and differ only in what `GL_RENDERER` comes back
as. They are reported as separate tiers anyway, because their performance
characteristics are two orders of magnitude apart and a caller who got the
middle one needs to know.

**Silent fallback is how people waste an afternoon blaming the wrong layer**, so
selection returns a chain, not just an answer: `select_backend()` attaches a
`SelectionReport` naming every tier it skipped and why, and
`python -m lobster.cli contract` prints it.

**The GPU backend is not written yet, deliberately.** ModernGL is the chosen
answer (D22) and the seam below is what it has to implement, but writing it on a
machine with no GPU, no display and no graphics library installed would be
shipping code that has never executed a single line. The interface is small
enough that filling it in is mechanical once there is something to test against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


class BackendError(Exception):
    """A backend that was asked for and cannot run. Says which and why."""


@dataclass(frozen=True)
class BackendInfo:
    """What a backend is and whether it can run here."""

    name: str
    available: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "available": self.available,
                "detail": self.detail}


@dataclass(frozen=True)
class SelectionReport:
    """What was chosen, and why everything better was not.

    Exists because a silent fallback costs somebody an afternoon of profiling
    the wrong layer. `summary()` is the one-line log form:

        GPU unavailable -> llvmpipe unavailable -> software
    """

    chosen: str
    skipped: Tuple[Tuple[str, str], ...] = ()

    def summary(self) -> str:
        return " -> ".join([name + " unavailable" for name, _ in self.skipped]
                           + [self.chosen])

    def to_dict(self) -> Dict[str, Any]:
        return {"chosen": self.chosen,
                "skipped": [{"name": n, "reason": r} for n, r in self.skipped],
                "summary": self.summary()}


#: Renderer strings that mean "this GL context is a CPU rasteriser". Mesa
#: reports llvmpipe or softpipe; some older stacks report swrast.
_SOFTWARE_GL_MARKERS = ("llvmpipe", "softpipe", "swrast", "software rasterizer")


def gl_renderer_string() -> Optional[str]:
    """What `GL_RENDERER` says here, or None if there is no GL at all.

    **Only the import half of this has ever executed on the build machine** -
    there is no graphics library installed, so `import moderngl` raises and the
    function returns None. The context half is written from ModernGL's
    documented standalone-context API and is marked no-cover rather than
    pretended about, which is the same honesty D22 applied to the backend
    itself: the machinery that chooses between tiers is fully exercised (see
    `probe_with`), the four lines that talk to a driver are not.
    """
    try:
        import moderngl                      # type: ignore
    except Exception:
        return None
    try:                                     # pragma: no cover - needs a GL stack
        ctx = moderngl.create_standalone_context()
        try:
            return str(ctx.info.get("GL_RENDERER", "")) or "unknown"
        finally:
            ctx.release()
    except Exception:                        # pragma: no cover - needs a GL stack
        # A GL stack that imports but cannot make a context (no display, no
        # driver, a headless box without EGL) is the same answer as no GL at
        # all: drop a tier. It is not an error - the chain exists for this.
        return None


def _is_software_gl(renderer: str) -> bool:
    low = renderer.lower()
    return any(marker in low for marker in _SOFTWARE_GL_MARKERS)


class RenderBackend:
    """What a renderer has to implement.

    Shaped for the GPU, not for the CPU, so that the GPU backend is natural and
    the software one adapts rather than the other way round: static geometry is
    uploaded once per cell residency, and a frame is camera state plus a list of
    what to draw.
    """

    #: short, stable identifier - `software`, `moderngl`, ...
    name: str = "abstract"

    @classmethod
    def available(cls) -> BackendInfo:      # pragma: no cover - overridden
        raise NotImplementedError

    def upload_cell(self, cell: Any) -> None:
        """Hand the backend a cell's static geometry. Idempotent.

        Called on cell load. A GPU backend builds vertex buffers here, once,
        rather than per frame - which is the whole reason this method exists on
        an interface a CPU rasteriser does not need.
        """

    def release_cell(self, cell_id: str) -> None:
        """Drop a cell's geometry. Called on unload, so residency and GPU
        memory stay in step with each other."""

    def render(self, draw_list: Any, cells_by_id: Dict[str, Any],
               settings: Any) -> Any:       # pragma: no cover - overridden
        """Draw a culled draw list. Returns a `Framebuffer`."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _software_backend():
    from .software_backend import SoftwareBackend
    return SoftwareBackend


def _gl_backend():
    """The chosen GPU answer (D22). Not implemented yet - see module docstring.

    Both GL tiers load this: llvmpipe is a driver under the same code, not a
    second renderer.
    """
    return None


HARDWARE_GL = "moderngl"
SOFTWARE_GL = "moderngl-llvmpipe"
PYTHON_RASTER = "software"

#: Preference order, best first. The first that reports available wins.
_CANDIDATES = ((HARDWARE_GL, _gl_backend),
               (SOFTWARE_GL, _gl_backend),
               (PYTHON_RASTER, _software_backend))

_UNIMPLEMENTED = (
    "the seam is defined and ModernGL is the chosen GPU backend "
    "(DECISIONS.md D22), but it has not been written against real hardware yet")


def probe_with(renderer: Optional[str]) -> List[BackendInfo]:
    """Every tier's availability, given what GL reports.

    Split out from `probe()` so the tier logic is testable without a GL stack.
    That matters more than it looks: this machine can only ever exercise the
    "no GL at all" branch, and a chain that has only been reasoned about is
    exactly what D22 was written about.
    """
    out: List[BackendInfo] = []
    gl_impl = _gl_backend()

    if renderer is None:
        gl_detail = ("no OpenGL here - `moderngl` does not import, or no "
                     "context could be created (no driver, no display)")
        hardware_ok = software_ok = False
    elif _is_software_gl(renderer):
        gl_detail = "GL present but software-rasterised: {0}".format(renderer)
        hardware_ok, software_ok = False, True
    else:
        gl_detail = "hardware GL: {0}".format(renderer)
        hardware_ok, software_ok = True, False

    for name, ok in ((HARDWARE_GL, hardware_ok), (SOFTWARE_GL, software_ok)):
        if ok and gl_impl is None:
            out.append(BackendInfo(name=name, available=False,
                                   detail="{0} - {1}".format(gl_detail,
                                                             _UNIMPLEMENTED)))
        elif ok:
            out.append(BackendInfo(name=name, available=True, detail=gl_detail))
        elif renderer is None:
            out.append(BackendInfo(name=name, available=False,
                                   detail=gl_detail))
        else:
            out.append(BackendInfo(
                name=name, available=False,
                detail="{0}, which is not this tier".format(gl_detail)))

    out.append(_software_backend().available())
    return out


def probe() -> List[BackendInfo]:
    """Every backend and whether it can run here. What the CLI reports."""
    return probe_with(gl_renderer_string())


def selection_report(prefer: Optional[str] = None,
                     infos: Optional[List[BackendInfo]] = None
                     ) -> SelectionReport:
    """Which tier wins here, and why each better one did not.

    Returned rather than logged, so a caller can put it wherever its logging
    lives. Lobster has no opinion about that (L7).
    """
    table = {info.name: info for info in (infos if infos is not None
                                          else probe())}
    if prefer is not None:
        info = table.get(prefer)
        if info is None:
            raise BackendError(
                "unknown render backend {0!r}; this build has {1}".format(
                    prefer, sorted(table)))
        if not info.available:
            raise BackendError(
                "render backend {0!r} was asked for but cannot run here: "
                "{1}".format(prefer, info.detail))
        return SelectionReport(chosen=prefer)

    skipped: List[Tuple[str, str]] = []
    for name, _loader in _CANDIDATES:
        info = table[name]
        if info.available:
            return SelectionReport(chosen=name, skipped=tuple(skipped))
        skipped.append((name, info.detail))
    raise BackendError(                      # pragma: no cover - the pure
        "no render backend is available: {0}".format(  # Python tier always is
            "; ".join("{0} ({1})".format(n, r) for n, r in skipped)))


def select_backend(prefer: Optional[str] = None) -> RenderBackend:
    """The best backend that can run here, carrying its own selection story.

    `prefer` names one explicitly and **raises** if it cannot run, rather than
    quietly falling back: a caller that asked for the GPU and silently got a
    Python rasteriser would draw the right picture and blame the wrong thing
    for the frame rate.

    The returned instance carries `.selection`, a `SelectionReport`, so the
    fallback chain can be logged at the point of use:

        backend = select_backend()
        log.info("render backend: %s", backend.selection.summary())
    """
    report = selection_report(prefer)
    backend = _instantiate(report.chosen)
    backend.selection = report
    return backend


def _instantiate(name: str) -> RenderBackend:
    for candidate, loader in _CANDIDATES:
        if candidate == name:
            backend = loader()
            if backend is None:
                raise BackendError(
                    "render backend {0!r} is not implemented".format(name))
            instance = backend()
            instance.name = name
            return instance
    raise BackendError("unknown render backend {0!r}".format(name))
