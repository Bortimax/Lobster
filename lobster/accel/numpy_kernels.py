"""NumPy implementations of the two seam kernels (D26's seam, D40).

Both mirror `lobster.conformance.REFERENCE` exactly in signature and in answer,
and the differential harness (D28) is what proves the second part rather than
this docstring.

**The vectorisation is over the thing that scales.** `segment_query` does one
point-to-segment distance for *every* entry at once, which is where a broad
phase spends its time; `nearest_region` does one segment-to-segment distance for
every bone at once. Both are the same clamped-parameter solutions
`lobster.geometry` uses, written with arrays instead of loops - so the maths is
identical and only the dispatch changes.

**Where the reference is authoritative, this follows it exactly**, including
things that look like details and are not:

* Candidates sort by `(distance, entity_id)`, not by distance alone.
* `nearest_region` breaks ties towards the **earlier** capsule, because the
  reference compares with a strict `<`. Ordering is part of the input.
* `precise` is "a capsule was intersected", decided by the same
  `gap <= radius` test rather than by a re-derived one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

import numpy as np

_EPS = 1e-12


def _segment_point_distance(a: np.ndarray, b: np.ndarray,
                            points: np.ndarray) -> np.ndarray:
    """Distance from every point to segment `ab`. Mirrors
    `geometry.closest_point_on_segment`, clamped identically."""
    ab = b - a
    denom = float(ab @ ab)
    if denom == 0.0:
        return np.linalg.norm(points - a, axis=1)
    t = ((points - a) @ ab) / denom
    np.clip(t, 0.0, 1.0, out=t)
    closest = a + t[:, None] * ab
    return np.linalg.norm(points - closest, axis=1)


def segment_query(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Which entries lie within `radius` of the segment, nearest first.

    The reference walks a grid to avoid testing everything; this tests
    everything, vectorised. That is not a shortcut - the grid exists because a
    Python loop over the whole cell is expensive, and the cost it was avoiding
    is the cost that vanishes here. The *answer* is identical either way,
    because the grid is only a broad phase and both paths finish with the same
    distance test.
    """
    entries = payload["entries"]
    if not entries:
        return {"hits": []}

    allowed = payload.get("tiers")
    ids = [e[0] for e in entries]
    positions = np.asarray([e[1] for e in entries], dtype=np.float64)
    keep = np.ones(len(entries), dtype=bool)
    if allowed:
        allowed = set(allowed)
        keep = np.asarray([e[2] in allowed for e in entries], dtype=bool)

    start = np.asarray(payload["start"], dtype=np.float64)
    end = np.asarray(payload["end"], dtype=np.float64)
    distances = _segment_point_distance(start, end, positions)

    hit = keep & (distances <= float(payload["radius"]))
    found = [(ids[i], float(distances[i])) for i in np.flatnonzero(hit)]
    # (distance, entity_id) - the reference's ordering, ties included.
    found.sort(key=lambda pair: (pair[1], pair[0]))
    return {"hits": [[eid, d] for eid, d in found]}


def _segment_segment_distance(p1: np.ndarray, q1: np.ndarray,
                              p2: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Shortest distance from one segment to each of many segments.

    The same clamped-parameter solution as `geometry.segment_segment_distance`,
    branch for branch - the degenerate cases are `np.where` instead of `if`,
    which is the only difference.
    """
    d1 = q1 - p1
    d2 = q2 - p2
    r = p1 - p2

    # d1 is one segment (3,); d2, r are per-capsule (N, 3). Every scalar in
    # the reference except `a` is therefore a column here - `c` included, which
    # the first draft got wrong by treating it as a scalar.
    a = float(d1 @ d1)
    e = np.einsum("ij,ij->i", d2, d2)
    f = np.einsum("ij,ij->i", d2, r)
    c = r @ d1
    b = d2 @ d1

    s = np.zeros_like(e)
    t = np.zeros_like(e)

    both_degenerate = (e <= _EPS) if a <= _EPS else np.zeros_like(e, dtype=bool)
    e_safe = np.where(e <= _EPS, 1.0, e)

    if a <= _EPS:
        t = np.clip(f / e_safe, 0.0, 1.0)
    else:
        denom = a * e - b * b
        s = np.where(denom != 0.0,
                     np.clip((b * f - c * e) / np.where(denom != 0.0, denom, 1.0),
                             0.0, 1.0),
                     0.0)
        t = (b * s + f) / e_safe
        low = t < 0.0
        high = t > 1.0
        s = np.where(low, np.clip(-c / a, 0.0, 1.0), s)
        s = np.where(high, np.clip((b - c) / a, 0.0, 1.0), s)
        t = np.where(low, 0.0, np.where(high, 1.0, t))
        # e ~ 0: the reference sets t = 0 and solves s against d1 alone.
        degenerate_e = e <= _EPS
        if np.any(degenerate_e):
            s = np.where(degenerate_e, np.clip(-c / a, 0.0, 1.0), s)
            t = np.where(degenerate_e, 0.0, t)

    c1 = p1 + s[:, None] * d1
    c2 = p2 + t[:, None] * d2
    out = np.linalg.norm(c1 - c2, axis=1)
    if np.any(both_degenerate):
        out = np.where(both_degenerate, np.linalg.norm(r, axis=1), out)
    return out


def nearest_region(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Which bone a ray struck, over an already-filtered capsule list."""
    boxes = payload["boxes"]
    if not boxes:
        return {"region": None, "precise": False}

    origin = np.asarray(payload["origin"], dtype=np.float64)
    direction = np.asarray(payload["direction"], dtype=np.float64)
    norm = float(np.linalg.norm(direction))
    heading = direction / norm if norm else direction
    far = origin + heading * float(payload["max_distance"])

    regions = [b[0] for b in boxes]
    a = np.asarray([b[1] for b in boxes], dtype=np.float64)
    b_pts = np.asarray([b[2] for b in boxes], dtype=np.float64)
    radii = np.asarray([b[3] for b in boxes], dtype=np.float64)

    gaps = _segment_segment_distance(origin, far, a, b_pts)
    struck = gaps <= radii

    if np.any(struck):
        centres = (a + b_pts) * 0.5
        along = np.linalg.norm(centres - origin, axis=1)
        # argmin returns the first minimum, which is the reference's strict `<`
        # tie-break: the earlier capsule wins.
        idx = int(np.flatnonzero(struck)[np.argmin(along[struck])])
        return {"region": regions[idx], "precise": True}

    return {"region": regions[int(np.argmin(gaps))], "precise": False}


def kernels() -> Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]]:
    from ..conformance import NEAREST_REGION, SEGMENT_QUERY
    return {SEGMENT_QUERY: segment_query, NEAREST_REGION: nearest_region}
