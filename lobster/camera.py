"""Camera and frustum (Scope 1, "a renderer sized for the actual target").

Pure geometry: where the viewer is, what it can see, and where a world point
lands on screen. No drawing, no device, no state beyond the pose - so this is
usable by a software rasteriser, a GPU renderer, a debug overlay or a test,
and none of them has to agree with the others about anything but the numbers.

Right-handed, Y up. View space keeps depth **positive** going away from the
camera so every comparison here and in the rasteriser is a plain `>`; the
conventional -Z flip belongs to whatever graphics stack consumes `project`.

Culling tests take a **bounding sphere**. A sphere test is cheap, orientation-
free, and errs outward: it can call something visible that is not, which costs
one wasted draw, and it can never cull something that is. That is the same
direction every other gate in Lobster errs (DECISIONS.md D19), for the same
reason - an over-inclusive gate wastes work, an under-inclusive one loses
things silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import (AABB, Vec3, add, cross, distance, dot,
                       normalize, scale, sub)

#: Sensible defaults for a cell-based, disc-era-budget game.
DEFAULT_FOV_Y_DEG = 60.0
DEFAULT_NEAR_M = 0.1

#: Far plane. A cell is 128 m and residency is "cell plus ring", so seeing
#: much past two cells is showing geometry that is not resident anyway.
DEFAULT_FAR_M = 320.0


class CameraError(Exception):
    pass


@dataclass(frozen=True)
class Camera:
    """A viewer. Immutable; `looking_at` and `moved_to` return new ones."""

    position: Vec3 = (0.0, 0.0, 0.0)
    #: unit vectors of the view basis, world space
    forward: Vec3 = (0.0, 0.0, -1.0)
    up: Vec3 = (0.0, 1.0, 0.0)
    fov_y_deg: float = DEFAULT_FOV_Y_DEG
    aspect: float = 16.0 / 9.0
    near: float = DEFAULT_NEAR_M
    far: float = DEFAULT_FAR_M

    def __post_init__(self) -> None:
        if not (0.0 < self.fov_y_deg < 180.0):
            raise CameraError(
                "fov_y_deg must be between 0 and 180, got {0}".format(
                    self.fov_y_deg))
        if self.near <= 0.0 or self.far <= self.near:
            raise CameraError(
                "near must be positive and far greater than near, got "
                "{0}..{1}".format(self.near, self.far))
        if self.aspect <= 0.0:
            raise CameraError("aspect must be positive")
        # Two pure functions of these immutable fields, computed once here.
        # Both were being recomputed inside `to_view` and `sees_sphere`, which
        # a cull calls once per drawable per cell per frame - so a 400-prop
        # cell orthonormalised the same basis three thousand six hundred times
        # a frame and called `math.tan` twice as often. Found by profiling the
        # per-placement cost the ASSET_SCOPE §6 budget is derived from (D51).
        forward = normalize(self.forward)
        right = normalize(cross(self.up, forward))
        object.__setattr__(self, "_basis",
                           (right, normalize(cross(forward, right)), forward))
        ty = math.tan(math.radians(self.fov_y_deg) * 0.5)
        object.__setattr__(self, "_tan_half_fov", (ty * self.aspect, ty))

    # -- construction --------------------------------------------------------
    @classmethod
    def looking_at(cls, position: Vec3, target: Vec3, **kwargs: Any) -> "Camera":
        forward = normalize(sub(target, position))
        if forward == (0.0, 0.0, 0.0):
            raise CameraError("camera position and target are the same point")
        world_up = (0.0, 1.0, 0.0)
        if abs(dot(forward, world_up)) > 0.999:      # looking straight up/down
            world_up = (0.0, 0.0, 1.0)
        right = normalize(cross(world_up, forward))
        up = normalize(cross(forward, right))
        return cls(position=position, forward=forward, up=up, **kwargs)

    def moved_to(self, position: Vec3) -> "Camera":
        return Camera(position=position, forward=self.forward, up=self.up,
                      fov_y_deg=self.fov_y_deg, aspect=self.aspect,
                      near=self.near, far=self.far)

    def with_aspect(self, aspect: float) -> "Camera":
        return Camera(position=self.position, forward=self.forward, up=self.up,
                      fov_y_deg=self.fov_y_deg, aspect=aspect,
                      near=self.near, far=self.far)

    # -- basis ---------------------------------------------------------------
    def right(self) -> Vec3:
        return normalize(cross(self.up, self.forward))

    def basis(self) -> Tuple[Vec3, Vec3, Vec3]:
        """(right, up, forward), orthonormalised. Computed once, at birth."""
        return self._basis

    # -- transforms ----------------------------------------------------------
    def to_view(self, point: Vec3) -> Vec3:
        """World point -> view space. +x right, +y up, **+z forward**.

        Depth is positive going away from the camera, which keeps every
        comparison in this file and the rasteriser a plain `>`; the -Z
        convention is applied at projection.
        """
        right, up, forward = self.basis()
        rel = sub(point, self.position)
        return (dot(rel, right), dot(rel, up), dot(rel, forward))

    def tan_half_fov(self) -> Tuple[float, float]:
        return self._tan_half_fov

    def project(self, point: Vec3, width: int, height: int
                ) -> Optional[Tuple[float, float, float]]:
        """World point -> (screen x, screen y, view depth), or None if behind.

        Screen space is pixels with the origin top-left, which is what an image
        buffer wants. None means "not projectable" - at or behind the near
        plane - and is a different answer from "off screen", which is a
        perfectly projectable point with coordinates outside the viewport.
        """
        view = self.to_view(point)
        if view[2] <= self.near:
            return None
        return self.project_view(view, width, height)

    def project_view(self, view: Vec3, width: int,
                     height: int) -> Tuple[float, float, float]:
        """Already-view-space point -> screen. No behind-camera check.

        Separate from `project` so a caller that has clipped against the near
        plane itself is not asked to round-trip back through world space to be
        told what it already knows.
        """
        tan_x, tan_y = self.tan_half_fov()
        ndc_x = view[0] / (view[2] * tan_x)
        ndc_y = view[1] / (view[2] * tan_y)
        return ((ndc_x * 0.5 + 0.5) * width,
                (0.5 - ndc_y * 0.5) * height,
                view[2])

    # -- culling -------------------------------------------------------------
    def sees_sphere(self, center: Vec3, radius: float) -> bool:
        """Is any part of this sphere inside the frustum?

        Conservative: a sphere straddling a plane counts as visible.
        """
        view = self.to_view(center)
        if view[2] + radius < self.near or view[2] - radius > self.far:
            return False
        tan_x, tan_y = self.tan_half_fov()
        # distance from the axis a point may be at this depth, plus the radius
        # inflated by the plane's slope
        limit_x = view[2] * tan_x + radius * math.sqrt(1.0 + tan_x * tan_x)
        if abs(view[0]) > limit_x:
            return False
        limit_y = view[2] * tan_y + radius * math.sqrt(1.0 + tan_y * tan_y)
        return abs(view[1]) <= limit_y

    def sees_aabb(self, box: AABB) -> bool:
        """Bounding-sphere test on a box. Cheap and outward-erring."""
        center = box.center()
        radius = distance(center, box.maximum)
        return self.sees_sphere(center, radius)

    def distance_to(self, point: Vec3) -> float:
        return distance(self.position, point)

    def to_dict(self) -> Dict[str, Any]:
        return {"position": list(self.position),
                "forward": list(normalize(self.forward)),
                "up": list(self.up), "fov_y_deg": self.fov_y_deg,
                "aspect": self.aspect, "near": self.near, "far": self.far}
