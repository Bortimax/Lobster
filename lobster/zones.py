"""Zone trigger volumes (Scope 4).

> **`Zone` -> trigger volume**, shape via `shape_ref`: `box`, `cylinder`, or
> `polygon`. **These three primitives are frozen as of this scope.** They're
> sufficient for every anticipated case; adding a fourth later is a deliberate
> decision with its own note, not a one-line addition - every new primitive is a
> new code path content can come to depend on.

Frozen means frozen: `ZONE_SHAPE_PRIMITIVES` is a tuple in `lobster.constants`,
the build-step lint rejects anything else by name, and `shape_from_dict` below
raises rather than falling back to a bounding box. A fourth primitive is a Scope
change and a DECISIONS.md entry, not a pull request.

What this module answers is strictly geometric: *is this point inside that
volume*. It does not answer who is in a Zone - Scope 4 is explicit that

> **What Lobster does not invent:** occupancy, scheduling, faction state -
> `queries.zone_occupants` already answers that.

so a caller that wants occupants asks Octopus, and a caller that wants "did the
player just walk into the ambush trigger" asks this.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import (Any, ClassVar, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

from .constants import ZONE_SHAPE_PRIMITIVES
from .geometry import AABB, Vec3, polygon_contains_2d, vec3

BOX = "box"
CYLINDER = "cylinder"
POLYGON = "polygon"


class ZoneShapeError(Exception):
    """A shape outside the frozen primitive set, or a malformed one."""


class ZoneShape:
    """Base: every primitive answers the same two questions.

    `kind` is a ClassVar rather than a field so the three primitives stay plain
    frozen dataclasses with their own arguments - a shared `kind` field would
    put a defaulted argument in front of every subclass's real ones.
    """

    kind: ClassVar[str] = ""

    def contains(self, point: Vec3) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def bounds(self) -> AABB:                 # pragma: no cover - overridden
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:      # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True)
class BoxShape(ZoneShape):
    center: Vec3
    size: Vec3
    kind: ClassVar[str] = BOX

    def bounds(self) -> AABB:
        half = tuple(c * 0.5 for c in self.size)
        return AABB(tuple(self.center[i] - half[i] for i in range(3)),
                    tuple(self.center[i] + half[i] for i in range(3)))

    def contains(self, point: Vec3) -> bool:
        return self.bounds().contains(point)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": BOX, "center": list(self.center),
                "size": list(self.size)}


@dataclass(frozen=True)
class CylinderShape(ZoneShape):
    center: Vec3
    radius: float
    height: float
    kind: ClassVar[str] = CYLINDER

    def bounds(self) -> AABB:
        half = self.height * 0.5
        return AABB((self.center[0] - self.radius, self.center[1] - half,
                     self.center[2] - self.radius),
                    (self.center[0] + self.radius, self.center[1] + half,
                     self.center[2] + self.radius))

    def contains(self, point: Vec3) -> bool:
        half = self.height * 0.5
        if not (self.center[1] - half <= point[1] <= self.center[1] + half):
            return False
        dx = point[0] - self.center[0]
        dz = point[2] - self.center[2]
        return dx * dx + dz * dz <= self.radius * self.radius

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": CYLINDER, "center": list(self.center),
                "radius": self.radius, "height": self.height}


@dataclass(frozen=True)
class PolygonShape(ZoneShape):
    """An extruded polygon on the XZ plane. Convexity is not required."""

    points: Tuple[Tuple[float, float], ...]
    min_y: float
    max_y: float
    kind: ClassVar[str] = POLYGON

    def bounds(self) -> AABB:
        xs = [p[0] for p in self.points]
        zs = [p[1] for p in self.points]
        return AABB((min(xs), self.min_y, min(zs)),
                    (max(xs), self.max_y, max(zs)))

    def contains(self, point: Vec3) -> bool:
        if not (self.min_y <= point[1] <= self.max_y):
            return False
        return polygon_contains_2d(self.points, point[0], point[2])

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": POLYGON, "points": [list(p) for p in self.points],
                "min_y": self.min_y, "max_y": self.max_y}


def shape_from_dict(raw: Optional[Mapping[str, Any]], *,
                    zone_id: Optional[str] = None) -> Optional[ZoneShape]:
    """Parse a `Zone.shape` field. None means the Zone has no trigger volume.

    A Zone without a shape is perfectly legal - Octopus's Zone is "a QUERY INDEX
    over existing data", and plenty of Zones only ever get asked about
    occupancy. Only a Zone somebody wants to *stand in* needs geometry.
    """
    if raw is None:
        return None
    who = "Zone {0!r}".format(zone_id) if zone_id else "zone shape"
    if not isinstance(raw, Mapping):
        raise ZoneShapeError("{0}: shape must be an object".format(who))
    kind = raw.get("kind")
    if kind not in ZONE_SHAPE_PRIMITIVES:
        raise ZoneShapeError(
            "{0}: shape kind {1!r} is not one of the frozen primitives {2}. "
            "Adding a fourth is a deliberate scope decision with its own note, "
            "not a one-line addition (Scope 4).".format(
                who, kind, list(ZONE_SHAPE_PRIMITIVES)))
    try:
        if kind == BOX:
            return BoxShape(center=vec3(raw["center"], what="center"),
                            size=vec3(raw["size"], what="size"))
        if kind == CYLINDER:
            return CylinderShape(center=vec3(raw["center"], what="center"),
                                 radius=float(raw["radius"]),
                                 height=float(raw["height"]))
        points = raw["points"]
        if not isinstance(points, (list, tuple)) or len(points) < 3:
            raise ZoneShapeError(
                "{0}: a polygon needs at least three points".format(who))
        return PolygonShape(
            points=tuple((float(p[0]), float(p[1])) for p in points),
            min_y=float(raw.get("min_y", 0.0)),
            max_y=float(raw.get("max_y", 0.0)))
    except (KeyError, TypeError, ValueError) as e:
        raise ZoneShapeError("{0}: malformed {1} shape ({2})".format(
            who, kind, e)) from e


@dataclass(frozen=True)
class ZoneVolume:
    """One Zone's trigger volume, bound to the cell its coordinates are in."""

    zone_id: str
    shape: ZoneShape
    cell_id: Optional[str] = None

    def contains(self, point: Vec3) -> bool:
        return self.shape.contains(point)

    def to_dict(self) -> Dict[str, Any]:
        return {"zone_id": self.zone_id, "cell_id": self.cell_id,
                "shape": self.shape.to_dict()}


def volumes_for_cell(view: Any, cell_id: str) -> List[ZoneVolume]:
    """Every Zone whose trigger volume is expressed in this cell's space.

    Read from the record layer, never from the bundle (DECISIONS.md D7), so a
    mod that re-shapes a zone does it as ordinary layered content.
    """
    out: List[ZoneVolume] = []
    for zone in view.records_of_type("Zone"):
        shape = shape_from_dict(zone.get("shape"), zone_id=zone["id"])
        if shape is None:
            continue
        where = zone.get("shape_location_ref")
        if where is not None and where != cell_id:
            continue
        if where is None and cell_id not in (zone.get("location_refs") or ()):
            continue
        out.append(ZoneVolume(zone_id=zone["id"], shape=shape, cell_id=cell_id))
    return out


def zones_containing(volumes: Sequence[ZoneVolume], point: Vec3) -> List[str]:
    """Which trigger volumes a point is inside. Sorted, so it is reproducible."""
    return sorted(v.zone_id for v in volumes if v.contains(point))
