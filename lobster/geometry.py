"""Geometry primitives. Pure math, no policy, no state.

Everything Lobster exposes across the contract is expressed in these types, so
they are deliberately small, immutable, and JSON-round-trippable: a transform
that crosses into an Octopus record or a .lobster_cell bundle has to be plain
data (Scope 13 invariant 1, "Geometry is data").

Units are metres, right-handed, Y up. Rotations are quaternions (x, y, z, w).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]

IDENTITY_QUAT: Quat = (0.0, 0.0, 0.0, 1.0)


def vec3(value: Any, *, what: str = "position") -> Vec3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("{0} must be [x, y, z], got {1!r}".format(what, value))
    return (float(value[0]), float(value[1]), float(value[2]))


def quat(value: Any) -> Quat:
    if value is None:
        return IDENTITY_QUAT
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("rotation must be [x, y, z, w], got {0!r}".format(value))
    q = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    n = math.sqrt(sum(c * c for c in q))
    if n == 0.0:
        raise ValueError("rotation quaternion has zero length")
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def length(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def distance(a: Vec3, b: Vec3) -> float:
    return length(sub(a, b))


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def normalize(a: Vec3) -> Vec3:
    n = length(a)
    if n == 0.0:
        return (0.0, 0.0, 0.0)
    return scale(a, 1.0 / n)


def quat_rotate(q: Quat, v: Vec3) -> Vec3:
    """Rotate a vector by a quaternion."""
    x, y, z, w = q
    # t = 2 * cross(q.xyz, v)
    tx = 2.0 * (y * v[2] - z * v[1])
    ty = 2.0 * (z * v[0] - x * v[2])
    tz = 2.0 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + (y * tz - z * ty),
            v[1] + w * ty + (z * tx - x * tz),
            v[2] + w * tz + (x * ty - y * tx))


def quat_mul(a: Quat, b: Quat) -> Quat:
    """`a` then `b` applied to a vector as `a * b`: rotate by `b`, then by `a`."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def quat_conjugate(q: Quat) -> Quat:
    return (-q[0], -q[1], -q[2], q[3])


@dataclass(frozen=True)
class Transform:
    """Position + rotation. The only placement type on the contract surface."""

    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Quat = IDENTITY_QUAT

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "Transform":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("transform must be an object, got {0!r}".format(raw))
        return cls(position=vec3(raw.get("position", (0.0, 0.0, 0.0))),
                   rotation=quat(raw.get("rotation")))

    def to_dict(self) -> Dict[str, Any]:
        return {"position": [float(c) for c in self.position],
                "rotation": [float(c) for c in self.rotation]}

    def apply(self, point: Vec3) -> Vec3:
        """Local point -> world point."""
        return add(self.position, quat_rotate(self.rotation, point))

    def inverse_apply(self, point: Vec3) -> Vec3:
        """World point -> local point."""
        return quat_rotate(quat_conjugate(self.rotation), sub(point, self.position))

    def rotate(self, direction: Vec3) -> Vec3:
        """Local direction -> world direction. Rotation only, no translation."""
        return quat_rotate(self.rotation, direction)

    def compose(self, inner: "Transform") -> "Transform":
        """`self` applied *after* `inner`, as one transform.

        `outer.compose(inner).apply(p)` equals `outer.apply(inner.apply(p))`,
        and a test asserts exactly that over random pairs.

        Both are rigid - rotation and translation, no scale, which is what every
        placement in this project is - so composing them is a quaternion product
        and one rotated vector. Building two 4x4 matrices and multiplying them
        gives the same answer for sixty-four multiply-adds instead of about
        twenty, and that difference is the whole per-placement cost of a frame
        (DECISIONS.md D51).
        """
        return Transform(
            position=add(self.position, quat_rotate(self.rotation,
                                                    inner.position)),
            rotation=quat_mul(self.rotation, inner.rotation))

    def inverse_rotate(self, direction: Vec3) -> Vec3:
        """World direction -> local direction.

        A ray has an origin and a direction, and they transform differently: the
        origin is a point and the direction is not. Transforming a direction
        with `inverse_apply` would subtract the placement's position from it,
        which quietly bends every cross-cell ray.
        """
        return quat_rotate(quat_conjugate(self.rotation), direction)


@dataclass(frozen=True)
class AABB:
    """Axis-aligned bounding box, min/max inclusive-exclusive on max."""

    minimum: Vec3
    maximum: Vec3

    @classmethod
    def from_points(cls, points: Iterable[Vec3]) -> "AABB":
        pts = list(points)
        if not pts:
            raise ValueError("AABB needs at least one point")
        lo = (min(p[0] for p in pts), min(p[1] for p in pts),
              min(p[2] for p in pts))
        hi = (max(p[0] for p in pts), max(p[1] for p in pts),
              max(p[2] for p in pts))
        return cls(lo, hi)

    def intersects(self, other: "AABB") -> bool:
        return all(self.minimum[i] <= other.maximum[i]
                   and other.minimum[i] <= self.maximum[i] for i in range(3))

    def contains(self, point: Vec3) -> bool:
        return all(self.minimum[i] <= point[i] <= self.maximum[i]
                   for i in range(3))

    def expanded(self, margin: float) -> "AABB":
        return AABB(tuple(c - margin for c in self.minimum),  # type: ignore[arg-type]
                    tuple(c + margin for c in self.maximum))  # type: ignore[arg-type]

    def center(self) -> Vec3:
        return tuple((self.minimum[i] + self.maximum[i]) * 0.5
                     for i in range(3))  # type: ignore[return-value]

    def to_dict(self) -> Dict[str, Any]:
        return {"min": list(self.minimum), "max": list(self.maximum)}


@dataclass(frozen=True)
class Capsule:
    """Segment + radius. The hitbox primitive (Scope 3, "per-bone capsule")."""

    a: Vec3
    b: Vec3
    radius: float

    def aabb(self) -> AABB:
        lo = tuple(min(self.a[i], self.b[i]) - self.radius for i in range(3))
        hi = tuple(max(self.a[i], self.b[i]) + self.radius for i in range(3))
        return AABB(lo, hi)  # type: ignore[arg-type]

    def contains(self, point: Vec3) -> bool:
        return closest_point_on_segment(self.a, self.b, point)[1] <= self.radius

    def to_dict(self) -> Dict[str, Any]:
        return {"a": list(self.a), "b": list(self.b), "radius": self.radius}


def closest_point_on_segment(a: Vec3, b: Vec3, p: Vec3) -> Tuple[Vec3, float]:
    """Closest point on segment ab to p, and the distance to it."""
    ab = sub(b, a)
    denom = dot(ab, ab)
    if denom == 0.0:
        return a, distance(a, p)
    t = dot(sub(p, a), ab) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    closest = add(a, scale(ab, t))
    return closest, distance(closest, p)


def segment_segment_distance(p1: Vec3, q1: Vec3, p2: Vec3, q2: Vec3) -> float:
    """Shortest distance between two segments. Used for capsule/capsule and
    swing-volume tests; the standard clamped-parameter solution."""
    d1 = sub(q1, p1)
    d2 = sub(q2, p2)
    r = sub(p1, p2)
    a = dot(d1, d1)
    e = dot(d2, d2)
    f = dot(d2, r)
    eps = 1e-12
    if a <= eps and e <= eps:
        return distance(p1, p2)
    if a <= eps:
        s, t = 0.0, f / e
        t = min(1.0, max(0.0, t))
    else:
        c = dot(d1, r)
        if e <= eps:
            t = 0.0
            s = min(1.0, max(0.0, -c / a))
        else:
            b = dot(d1, d2)
            denom = a * e - b * b
            if denom != 0.0:
                s = min(1.0, max(0.0, (b * f - c * e) / denom))
            else:
                s = 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = min(1.0, max(0.0, (b - c) / a))
    c1 = add(p1, scale(d1, s))
    c2 = add(p2, scale(d2, t))
    return distance(c1, c2)


def ray_capsule_hit(origin: Vec3, direction: Vec3, capsule: Capsule,
                    max_distance: float) -> bool:
    """Does a ray of finite length come within `radius` of the capsule segment?

    Conservative and cheap: the ray is treated as a segment and the test is
    segment-segment distance. Exact enough for a whole-body capsule at
    PROJECTILE tier, which is the only place this is used.
    """
    end = add(origin, scale(normalize(direction), max_distance))
    return segment_segment_distance(origin, end, capsule.a,
                                    capsule.b) <= capsule.radius


def capsules_overlap(a: Capsule, b: Capsule) -> bool:
    return segment_segment_distance(a.a, a.b, b.a, b.b) <= a.radius + b.radius


def segment_aabb_overlap(start: Vec3, end: Vec3, box: AABB) -> bool:
    """Does a segment touch a box? The standard slab test.

    A bounding *sphere* is the wrong bound for a cell: a 128 m square that is
    barely any height gets a 90 m radius, so a shot fired away from a cell still
    "reaches" it and pays a broad-phase sweep for nothing. Cells are
    axis-aligned by construction, so the box is both cheap and tight.
    """
    t_min, t_max = 0.0, 1.0
    for axis in range(3):
        origin = start[axis]
        delta = end[axis] - origin
        lo, hi = box.minimum[axis], box.maximum[axis]
        if abs(delta) < 1e-12:
            if origin < lo or origin > hi:
                return False
            continue
        near = (lo - origin) / delta
        far = (hi - origin) / delta
        if near > far:
            near, far = far, near
        t_min = max(t_min, near)
        t_max = min(t_max, far)
        if t_min > t_max:
            return False
    return True


def sphere_aabb_overlap(center: Vec3, radius: float, box: AABB) -> bool:
    total = 0.0
    for i in range(3):
        v = center[i]
        if v < box.minimum[i]:
            total += (box.minimum[i] - v) ** 2
        elif v > box.maximum[i]:
            total += (v - box.maximum[i]) ** 2
    return total <= radius * radius


def polygon_contains_2d(points: Sequence[Tuple[float, float]],
                        x: float, z: float) -> bool:
    """Even-odd point-in-polygon on the XZ plane."""
    inside = False
    n = len(points)
    for i in range(n):
        x1, z1 = points[i]
        x2, z2 = points[(i + 1) % n]
        if (z1 > z) != (z2 > z):
            t = (z - z1) / (z2 - z1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def bounds_of(points: Iterable[Vec3]) -> AABB:
    return AABB.from_points(points)


def flatten_transforms(transforms: Iterable[Transform]) -> List[Dict[str, Any]]:
    return [t.to_dict() for t in transforms]
