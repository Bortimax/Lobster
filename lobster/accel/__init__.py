"""The accelerator seam, with an implementation behind it (D26, D40).

> **The hottest loops become replaceable by an optional native module.** When it
> is present it is used; when it is absent the pure-Python path runs and the
> whole suite still passes.

That is D26's commitment. This package is the registry that makes it real, and
`numpy_kernels` is the first implementation to sit behind it.

**On what counts as "native".** D26 named no toolchain on purpose, and this
machine has none - no `rustc`, no `cargo`, no `cl`, no `gcc`, no `clang`, no
Cython. Writing a Rust or C++ kernel here would ship code that has never
executed a line, which is the failure mode D22 exists to name. NumPy is a
compiled C extension with SIMD (SSE/AVX on this box) that **is** installed, it
releases the GIL inside its array loops, and it can be run and measured today.
So it is the implementation that exists; a Rust or C++ kernel remains open and
needs a toolchain, not a decision.

**Core Lobster keeps no hard dependency.** Nothing here is imported by the
runtime path; `available()` reports what can run, and the pure-Python kernels
stay authoritative (`conformance.AUTHORITY`).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

#: Registered implementations, best last-resort first. `python` is always here.
_IMPLEMENTATIONS: Dict[str, Callable[[], Optional[Dict[str, Callable]]]] = {}


class AccelError(Exception):
    pass


def register(name: str, loader: Callable[[], Optional[Dict[str, Callable]]]
             ) -> None:
    _IMPLEMENTATIONS[name] = loader


def _python_kernels() -> Dict[str, Callable]:
    from ..conformance import REFERENCE
    return dict(REFERENCE)


def _numpy_kernels() -> Optional[Dict[str, Callable]]:
    try:
        from . import numpy_kernels
    except Exception:
        return None
    return numpy_kernels.kernels()


def _native_kernels():
    """The C extension, if it has been built for this interpreter.

    Not committed as a binary and not built automatically: a machine that has
    not run `setup.py build_ext` simply does not have it, and the chain drops
    to the next implementation without drama (D42).
    """
    try:
        from .native import lobster_accel
    except Exception:
        return None
    from ..conformance import NEAREST_REGION, PLACE_BATCH, SEGMENT_QUERY
    return {SEGMENT_QUERY: lobster_accel.segment_query,
            NEAREST_REGION: lobster_accel.nearest_region,
            PLACE_BATCH: lobster_accel.place_batch}


register("python", _python_kernels)
register("numpy", _numpy_kernels)
register("native", _native_kernels)

#: Preference order for `select()`. Python is the floor and never fails.
PREFERENCE: Tuple[str, ...] = ("native", "numpy", "python")

#: **Per kernel, because measurement said so** (D40, D42).
#:
#: The seam is not one switch. Measured per call, against the reference:
#:
#: | kernel | numpy | native (C) |
#: |---|---|---|
#: | `segment_query`, 1,000 entities | 9.6x | **79x** |
#: | `nearest_region`, 6 bones | **0.46x** | **64x** |
#: | `place_batch`, 800 in one call | 8.2x | **50x** |
#: | `place_batch`, **a real frame** - 36 small calls | **0.26x** | **27x** |
#:
#: NumPy *loses* the refinement, because a rig has six bones and building six
#: arrays costs more than looping over six capsules - D26's seam-granularity
#: argument arriving one level down than it was aimed. C has no per-call array
#: to build and wins both.
#:
#: **`place_batch` is the same finding a third time, and worth reading twice.**
#: One batch of 800 is numpy's best case and it wins 8.2x there. But the kernel
#: is called once per resident cell per model, so a real frame is dozens of
#: batches of ten - and across an exterior ring numpy comes out at 10.0 ms
#: against the reference's 2.6 ms. Not marginally worse: **four times worse
#: than not accelerating at all.** The granularity that makes C fast is exactly
#: the granularity that makes numpy slow, and measuring the aggregate rather
#: than the single call is what showed it (D53).
#:
#: So numpy stays as the middle rung for the broad phase, where it is a real
#: win on a machine with no compiler, and is **excluded from the refinement and
#: from placement** rather than left in to be slower than the reference. It
#: still *implements* both, so the differential suite holds it to the same
#: answers and a caller may ask for it by name.
KERNEL_PREFERENCE: Dict[str, Tuple[str, ...]] = {
    "segment_query": ("native", "numpy", "python"),
    "nearest_region": ("native", "python"),
    "place_batch": ("native", "python"),
}


def available() -> List[Dict[str, Any]]:
    """Every registered implementation and whether it can run here.

    The same shape as `render.probe()`, and for the same reason: a caller that
    wonders why a volley is slow should be able to read which path is live
    rather than guess (D29).
    """
    out: List[Dict[str, Any]] = []
    for name in PREFERENCE:
        loader = _IMPLEMENTATIONS[name]
        try:
            kernels = loader()
        except Exception as exc:                      # pragma: no cover
            out.append({"name": name, "available": False,
                        "detail": "loader raised {0}".format(exc)})
            continue
        if kernels is None:
            out.append({"name": name, "available": False,
                        "detail": "not importable here"})
        else:
            out.append({"name": name, "available": True,
                        "detail": "provides {0}".format(sorted(kernels))})
    return out


def select(prefer: Optional[str] = None) -> Tuple[str, Dict[str, Callable]]:
    """`(name, kernels)` for the fastest kernel available for each operation.

    **Composed per kernel, not per implementation.** `KERNEL_PREFERENCE` says
    why: numpy wins the broad phase and loses the refinement, so taking either
    one wholesale would be slower than the mix.

    `prefer` names one explicitly and **raises** if it cannot run, rather than
    falling back quietly - the same rule `select_backend` follows, for the same
    reason: a caller who asked for the fast path and silently got the slow one
    would blame the wrong layer for the frame rate.
    """
    if prefer is not None:
        loader = _IMPLEMENTATIONS.get(prefer)
        if loader is None:
            raise AccelError(
                "unknown accelerator {0!r}; this build has {1}".format(
                    prefer, sorted(_IMPLEMENTATIONS)))
        kernels = loader()
        if kernels is None:
            raise AccelError(
                "accelerator {0!r} was asked for but cannot run here".format(
                    prefer))
        return prefer, kernels

    loaded: Dict[str, Optional[Dict[str, Callable]]] = {}

    def kernels_for(impl: str) -> Optional[Dict[str, Callable]]:
        if impl not in loaded:
            loaded[impl] = _IMPLEMENTATIONS[impl]()
        return loaded[impl]

    chosen: Dict[str, Callable] = {}
    picked: Dict[str, str] = {}
    for kernel, order in KERNEL_PREFERENCE.items():
        for impl in order:
            table = kernels_for(impl)
            if table is not None and kernel in table:
                chosen[kernel] = table[kernel]
                picked[kernel] = impl
                break
        else:                                          # pragma: no cover
            raise AccelError(
                "no implementation provides {0!r}".format(kernel))

    # The name describes what was actually composed, because "numpy" would be
    # a lie when half of it is the reference.
    name = "+".join(sorted(set(picked.values()))) if len(
        set(picked.values())) > 1 else next(iter(picked.values()))
    return name, chosen
