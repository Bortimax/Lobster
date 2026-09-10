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
from typing import Any, Dict, List, Optional, Sequence, Tuple


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


#: Context attempts, in order. A headless box is the case this list exists for.
#:
#: The platform default first, because on a workstation it is one call and it
#: is the one that finds the real GPU. **Then EGL explicitly**, which is how you
#: get a context with no display server at all - over SSH, in a container
#: without an X socket, in CI. `moderngl.create_context(standalone=True,
#: backend="egl")` needs no window and no `DISPLAY`, so "no display" is not the
#: same as "no GL" and must not be reported as if it were.
_CONTEXT_ATTEMPTS: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    ("platform default", {"standalone": True}),
    ("egl (headless)", {"standalone": True, "backend": "egl"}),
)


#: Memoised, and that is a correctness fix rather than a speed one.
#:
#: `gl_probe` creates a standalone context and releases it. Doing that while a
#: backend is **rendering** tears the live context out from under it: the first
#: frame after a probe came back entirely black, on real hardware, because
#: `render_resident` -> `select_backend` -> `probe` ran between two reads of a
#: framebuffer that was perfectly good a moment earlier.
#:
#: Probing once is also simply correct: which GL is installed does not change
#: during a process, so the second answer could only ever be the first one
#: again - bought with a context creation and a teardown.
_PROBE_CACHE: List[Tuple[Optional[str], str]] = []


def prime_gl_probe(renderer: Optional[str], detail: str) -> None:
    """Record what GL is, from a context somebody already made.

    Memoising alone was not enough: in a fresh process the first `probe()` is
    still a *first* call, so a backend that starts rendering and only later
    triggers a probe still gets its context torn down. A live context already
    knows its own `GL_RENDERER`, so `ModernGLBackend` primes this on creation
    and no probe ever needs to build a second one.
    """
    if not _PROBE_CACHE:
        _PROBE_CACHE.append((renderer, detail))


def forget_gl_probe() -> None:
    """Drop the memoised answer. For tests, and for a caller that has just
    installed a driver and wants to be believed."""
    del _PROBE_CACHE[:]


def gl_probe() -> Tuple[Optional[str], str]:
    """`(GL_RENDERER, detail)` - what GL is here, or None and why not.

    Three outcomes, kept distinct because they need different answers from
    whoever reads the log:

    * **No graphics library.** `moderngl` does not import. Install it.
    * **Library, no context.** Every backend failed, and the detail names each
      one and its error. On Linux that usually means no GPU *and* no Mesa, and
      the remedy is a package, not a code change.
    * **A context.** The renderer string decides which tier (D29).

    An earlier version tried a single unnamed backend and collapsed every
    failure into "no OpenGL here". On a headless machine that reported no GL at
    all when EGL would have given a perfectly good llvmpipe context - dropping
    two tiers and blaming the wrong layer, which is the exact failure D29 was
    written to prevent.

    **Only the import branch has ever executed on the build machine**, which has
    no graphics library. The attempt loop is marked no-cover rather than
    pretended about; what *is* exercised is everything downstream of this, which
    takes the result as an argument (`probe_with`).
    """
    if _PROBE_CACHE:
        return _PROBE_CACHE[0]
    answer = _gl_probe_uncached()
    _PROBE_CACHE.append(answer)
    return answer


def _gl_probe_uncached() -> Tuple[Optional[str], str]:
    try:
        import moderngl                      # type: ignore
    except Exception as exc:
        return None, ("no graphics library: `import moderngl` failed "
                      "({0})".format(exc.__class__.__name__))

    failures = []
    for label, kwargs in _CONTEXT_ATTEMPTS:  # pragma: no cover - needs a GL stack
        try:
            ctx = moderngl.create_context(**kwargs)
        except Exception as exc:
            failures.append("{0}: {1}".format(label, exc))
            continue
        try:
            renderer = str(ctx.info.get("GL_RENDERER", "")) or "unknown"
        finally:
            try:
                ctx.release()
            except Exception:
                pass
        return renderer, "context via {0}".format(label)

    return None, (                           # pragma: no cover - needs moderngl
        "moderngl imports but no backend gave a context - {0}. A headless "
        "machine still gets GL through EGL with Mesa installed, so this is a "
        "missing driver or library rather than a missing display".format(
            "; ".join(failures)))


def gl_renderer_string() -> Optional[str]:
    """Just the renderer string. `gl_probe` carries the reason as well."""
    return gl_probe()[0]


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

    def upload_structure(self, cell: Any, structure_id: str,
                         chunk_indices: Sequence[int]) -> None:
        """Refresh one structure after a chunk was destroyed.

        The only geometry that changes at runtime. A cell's meshes are static
        for its residency *except* that bringing a wall down moves
        `geometry_version` on that frame, and a re-upload of the touched
        structure is cheaper than one of the cell.
        """

    def upload_model(self, mesh: Any) -> None:
        """Hand the backend one library model. Idempotent by `model_ref`.

        Called when the *first* resident cell references it, and not again
        while any still does. That is the whole point of the library: fifty
        barrels in a town are fifty placements of one buffer (ASSET_SCOPE §2).
        """

    def release_model(self, model_ref: str) -> None:
        """Drop one model's geometry, when the last cell referencing it goes.

        A CPU backend has nothing to drop and may ignore both of these; a GPU
        one must not, because a model that is never released draws perfectly
        while the memory grows (D43).
        """

    def attach(self, bus: Any, manager: Any) -> Any:
        """Bind this backend's buffer lifetime to residency (RENDER_SCOPE §4).

        Subscribes to `on_enter_cell`, `on_exit_cell` and
        `on_structure_damaged`. Nothing calls `upload_cell` directly, and
        `CellManager` learns nothing about rendering - residency is geometry,
        and reaching from there into presentation is the L8 mistake at a
        different boundary.
        """
        from .residency import GpuResidency
        return GpuResidency(self, manager).attach(bus)

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
    """The chosen GPU answer (D22), and now written.

    Both GL tiers load this: llvmpipe is a driver under the same code, not a
    second renderer, which is why the two tiers differ only in what
    `GL_RENDERER` came back as.
    """
    try:
        from .gl_backend import ModernGLBackend
    except Exception:
        return None
    return ModernGLBackend


HARDWARE_GL = "moderngl"
SOFTWARE_GL = "moderngl-llvmpipe"
PYTHON_RASTER = "software"

#: Preference order, best first. The first that reports available wins.
_CANDIDATES = ((HARDWARE_GL, _gl_backend),
               (SOFTWARE_GL, _gl_backend),
               (PYTHON_RASTER, _software_backend))

_UNIMPLEMENTED = (
    "the moderngl package is not importable, so the GL backend cannot load")


def probe_with(renderer: Optional[str],
               reason: Optional[str] = None) -> List[BackendInfo]:
    """Every tier's availability, given what GL reports.

    Split out from `probe()` so the tier logic is testable without a GL stack.
    That matters more than it looks: this machine can only ever exercise the
    "no GL at all" branch, and a chain that has only been reasoned about is
    exactly what D22 was written about.
    """
    out: List[BackendInfo] = []
    gl_impl = _gl_backend()

    if renderer is None:
        gl_detail = reason or (
            "no OpenGL here - `moderngl` does not import, or no backend gave "
            "a context")
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
    return probe_with(*gl_probe())


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
