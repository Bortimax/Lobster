"""NumPy implementations of the four seam kernels (D26's seam, D40, D53, D54).

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
* `place_batch` returns survivors **in input order**, and packs f32 in the
  column-major order GLSL wants.

**`place_batch` is the one this library was made for.** `nearest_region` loses
to the reference because a rig has six bones and building six arrays costs more
than looping over them (D40) - the seam-granularity argument arriving one level
down. A placement batch is tens to hundreds of rows of identical work, which is
the far side of that same trade.
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


def _quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """`geometry.quat_rotate`, over a stack of vectors."""
    u, s = q[:3], q[3]
    return (2.0 * (u @ v.T)[:, None] * u
            + (s * s - u @ u) * v
            + 2.0 * s * np.cross(np.broadcast_to(u, v.shape), v))


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """`geometry.quat_mul`, `a` against a stack of `b`. Order is not
    symmetric and the reference composes outer-then-inner."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ], axis=1)


def place_batch(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Cull one cell's placements and pack what survived.

    Every step is over the whole batch at once: one composition, one frustum
    test, one matrix build, one `tobytes`. The float64 maths is the reference's
    and only the last step narrows, which is the same thing the native kernel
    does and the reason both land inside the declared tolerance.
    """
    from ..conformance import INSTANCE_FLOATS, PLACEMENT_FLOATS

    flat = np.asarray(payload["placements"], dtype=np.float64)
    total = flat.size // PLACEMENT_FLOATS
    if total == 0:
        return {"visible": [], "centers": [], "distances": [],
                "instances": b""}
    rows = flat.reshape(total, PLACEMENT_FLOATS)
    local_pos, local_rot = rows[:, 0:3], rows[:, 3:7]

    cell = payload["cell"]
    cell_pos = np.asarray(cell["position"], dtype=np.float64)
    cell_rot = np.asarray(cell["rotation"], dtype=np.float64)

    centres = cell_pos + _quat_rotate(cell_rot, local_pos)
    rot = _quat_mul(cell_rot, local_rot)

    # Camera.__post_init__, then Camera.sees_sphere - the same four
    # comparisons, evaluated for every row at once.
    cam = payload["camera"]
    position = np.asarray(cam["position"], dtype=np.float64)
    forward = np.asarray(cam["forward"], dtype=np.float64)
    up_in = np.asarray(cam["up"], dtype=np.float64)
    forward = forward / (np.linalg.norm(forward) or 1.0)
    right = np.cross(up_in, forward)
    right = right / (np.linalg.norm(right) or 1.0)
    up = np.cross(forward, right)
    up = up / (np.linalg.norm(up) or 1.0)
    tan_y = np.tan(np.radians(float(cam["fov_y_deg"])) * 0.5)
    tan_x = tan_y * float(cam["aspect"])
    near, far = float(cam["near"]), float(cam["far"])
    radius = float(payload["radius"])

    rel = centres - position
    vx, vy, vz = rel @ right, rel @ up, rel @ forward
    keep = (vz + radius >= near) & (vz - radius <= far)
    keep &= np.abs(vx) <= vz * tan_x + radius * np.sqrt(1.0 + tan_x * tan_x)
    keep &= np.abs(vy) <= vz * tan_y + radius * np.sqrt(1.0 + tan_y * tan_y)

    visible = np.flatnonzero(keep)
    if visible.size == 0:
        return {"visible": [], "centers": [], "distances": [],
                "instances": b""}
    kept_centres = centres[visible]
    distances = np.linalg.norm(rel[visible], axis=1)

    x, y, z, w = (rot[visible, 0], rot[visible, 1], rot[visible, 2],
                  rot[visible, 3])
    packed = np.zeros((visible.size, INSTANCE_FLOATS), dtype=np.float64)
    # Column-major, which is what `pack_matrix4` produces and GLSL reads.
    packed[:, 0] = 1 - 2 * (y * y + z * z)
    packed[:, 1] = 2 * (x * y + z * w)
    packed[:, 2] = 2 * (x * z - y * w)
    packed[:, 4] = 2 * (x * y - z * w)
    packed[:, 5] = 1 - 2 * (x * x + z * z)
    packed[:, 6] = 2 * (y * z + x * w)
    packed[:, 8] = 2 * (x * z + y * w)
    packed[:, 9] = 2 * (y * z - x * w)
    packed[:, 10] = 1 - 2 * (x * x + y * y)
    packed[:, 12:15] = kept_centres
    packed[:, 15] = 1.0
    packed[:, 16] = _sample_light(payload.get("lightmap"), kept_centres)

    return {"visible": [int(i) for i in visible],
            "centers": kept_centres.tolist(),
            "distances": [float(d) for d in distances],
            "instances": packed.astype(np.float32).tobytes()}


def _sample_light(lightmap: Any, points: np.ndarray) -> np.ndarray:
    """`ResidentCell.ambient_at`, over a stack of points.

    `np.floor_divide` on floats floors towards negative infinity, which is what
    Python's `//` does and what a C cast does *not* - the same trap the native
    kernel calls out.
    """
    ones = np.ones(points.shape[0], dtype=np.float64)
    if not lightmap:
        return ones
    data = lightmap.get("data")
    side = int(lightmap.get("side") or 0)
    if data is None or side <= 0 or len(data) == 0:
        return ones
    voxel = float(lightmap.get("voxel_size") or 1.0) or 1.0
    table = np.frombuffer(data, dtype=np.uint8) if isinstance(
        data, (bytes, bytearray)) else np.asarray(data, dtype=np.uint8)
    ix = np.clip(np.floor(points[:, 0] / voxel).astype(np.int64), 0, side - 1)
    iz = np.clip(np.floor(points[:, 2] / voxel).astype(np.int64), 0, side - 1)
    index = iz * side + ix
    inside = index < table.size
    out = ones.copy()
    out[inside] = table[index[inside]] / 255.0
    return out


def _quat_rotate_rows(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """`geometry.quat_rotate` with a *different* quaternion per row."""
    u, s = q[:, :3], q[:, 3:4]
    uv = np.sum(u * v, axis=1, keepdims=True)
    uu = np.sum(u * u, axis=1, keepdims=True)
    return 2.0 * uv * u + (s * s - uu) * v + 2.0 * s * np.cross(u, v)


def pose_capsules(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Where a posed rig's selected bones are, in world space.

    Answers in float64 and not f32: the consumer is `nearest_region`, which
    works in doubles.
    """
    from ..conformance import CAPSULE_FLOATS, PLACEMENT_FLOATS

    rest = np.asarray(payload["rest"], dtype=np.float64)
    pose = np.asarray(payload["pose"], dtype=np.float64)
    total = rest.size // CAPSULE_FLOATS
    selected = payload.get("bones")

    if total == 0 or (selected is not None and len(selected) == 0):
        return {"capsules": b""}
    rest = rest.reshape(total, CAPSULE_FLOATS)
    pose = pose.reshape(pose.size // PLACEMENT_FLOATS, PLACEMENT_FLOATS)
    if selected is None:
        index = np.arange(total)
    else:
        index = np.asarray(selected, dtype=np.int64)
        # Refused, not wrapped - numpy would have served the last bone for -1,
        # which is the same silent substitution the reference now rejects.
        if index.min() < 0 or index.max() >= total:
            raise IndexError(
                "bone index outside this rig's {0} bones".format(total))

    root = payload["root"]
    root_pos = np.asarray(root["position"], dtype=np.float64)
    root_rot = np.asarray(root["rotation"], dtype=np.float64)

    # Transform.compose(root, local), for every selected bone at once.
    origin = root_pos + _quat_rotate(root_rot, pose[index, 0:3])
    rot = _quat_mul(root_rot, pose[index, 3:7])

    out = np.empty((index.size, CAPSULE_FLOATS), dtype=np.float64)
    out[:, 0:3] = origin + _quat_rotate_rows(rot, rest[index, 0:3])
    out[:, 3:6] = origin + _quat_rotate_rows(rot, rest[index, 3:6])
    out[:, 6] = rest[index, 6]
    return {"capsules": out.tobytes()}


def kernels() -> Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]]:
    from ..conformance import (NEAREST_REGION, PLACE_BATCH, POSE_CAPSULES,
                               SEGMENT_QUERY)
    return {SEGMENT_QUERY: segment_query, NEAREST_REGION: nearest_region,
            PLACE_BATCH: place_batch, POSE_CAPSULES: pose_capsules}
