"""Both model kinds, meshed to one vertex format (ASSET_SCOPE 1, 2).

`voxel` models go through the **existing** `StructureMesher` - not a copy of it,
not a variant of it. That is deliberate and it is the opposite of D11: terrain
and structures duplicate a greedy mesher because Scope 15.1 forbids them a
shared code path, and a *model* is a structure's voxel grid with no break-state,
so it is the one case where sharing is the correct answer rather than the lazy
one.

`primitive` models go through the generators below, which are the "dozen-line
generator" ASSET_SCOPE 7 asked for and are deliberately not more than that.

Everything ends as the same nine floats a vertex - `position, normal, tint` -
because the whole justification for two kinds was that neither teaches anything
downstream a second representation.

## The three conversions that happen here and nowhere else

**Palette to colour.** A `.vox` file carries the 256-entry palette the author
actually saw in MagicaVoxel, and a build step is exactly where that gets
resolved: a model in the library holds RGB, never an index, so nothing at
runtime has to know *which* palette a model meant. Files with no `RGBA` chunk
(MagicaVoxel omits it when the default palette is untouched) and primitives -
which have no palette of their own - fall back to `DEFAULT_PALETTE`, the same
one structures are drawn with.

**Anchoring.** Geometry is centred on X and Z and sits on y = 0. The reason is
rotation, not tidiness: a placement transform rotates about the model origin, so
a corner-origin model swings around its corner instead of turning in place, and
"turn the crate 45 degrees" would move it as well. The anchor is taken from the
*meshed* bounds, so padding a `.vox` up to a legal cube cannot shift a model.

**Lighting: none.** A cell bundle multiplies its baked lightmap into its vertex
tints, which is free and correct because a cell's light cannot change during a
residency. A library model is shared by every cell that places it, so it carries
its material colour unlit and the placement's cell supplies the rest.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..constants import (CYLINDER_SEGMENTS, MODEL_PRIMITIVE, MODEL_VOXEL,
                         PRIMITIVE_SHAPES, VOXEL_SIZE_M)
from ..geometry import AABB, Vec3
from ..model_library import ModelMesh
from ..structure_mesher import StructureMesher
from ..structures import LiveStructure
from .vox import VoxError, VoxModel, read_vox, to_structure

Rgb = Tuple[float, float, float]


@dataclass(frozen=True)
class Triangle:
    """One triangle on its way to the vertex buffer.

    Build-time scaffolding, not a representation: nothing outside this module
    ever sees one, and what leaves is packed floats.
    """

    material: int
    normal: Vec3
    corners: Tuple[Vec3, Vec3, Vec3]


# ---------------------------------------------------------------------------
# Primitive generators
# ---------------------------------------------------------------------------

def _quads_to_triangles(material: int, normal: Vec3,
                        corners: Sequence[Vec3]) -> List[Triangle]:
    """A convex quad as two triangles, the same split the GL packer uses."""
    return [Triangle(material, normal,
                     (corners[a], corners[b], corners[c]))
            for a, b, c in ((0, 1, 2), (0, 2, 3))]


def box_triangles(size: Sequence[float], material: int) -> List[Triangle]:
    """Six faces, twelve triangles, spanning 0..size before anchoring."""
    sx, sy, sz = (float(v) for v in size)
    faces: Tuple[Tuple[Vec3, Tuple[Vec3, ...]], ...] = (
        ((0.0, 0.0, 1.0), ((0, 0, sz), (sx, 0, sz), (sx, sy, sz), (0, sy, sz))),
        ((0.0, 0.0, -1.0), ((sx, 0, 0), (0, 0, 0), (0, sy, 0), (sx, sy, 0))),
        ((1.0, 0.0, 0.0), ((sx, 0, sz), (sx, 0, 0), (sx, sy, 0), (sx, sy, sz))),
        ((-1.0, 0.0, 0.0), ((0, 0, 0), (0, 0, sz), (0, sy, sz), (0, sy, 0))),
        ((0.0, 1.0, 0.0), ((0, sy, sz), (sx, sy, sz), (sx, sy, 0), (0, sy, 0))),
        ((0.0, -1.0, 0.0), ((0, 0, 0), (sx, 0, 0), (sx, 0, sz), (0, 0, sz))),
    )
    out: List[Triangle] = []
    for normal, corners in faces:
        out.extend(_quads_to_triangles(
            material, normal, [(float(x), float(y), float(z))
                               for x, y, z in corners]))
    return out


def cylinder_triangles(radius: float, height: float,
                       material: int) -> List[Triangle]:
    """A capped cylinder about the Y axis, base on y = 0.

    `CYLINDER_SEGMENTS` sides, fixed rather than authored: see the constant.
    """
    r, h = float(radius), float(height)
    n = CYLINDER_SEGMENTS
    ring = [(math.cos(2.0 * math.pi * i / n), math.sin(2.0 * math.pi * i / n))
            for i in range(n)]
    out: List[Triangle] = []
    for i in range(n):
        cx, cz = ring[i]
        nx, nz = ring[(i + 1) % n]
        # One outward normal per side face: flat shading, like every other
        # surface this project draws. A smoothed cylinder would need per-vertex
        # normals and would be the only smooth thing in the world.
        mx, mz = (cx + nx), (cz + nz)
        length = math.hypot(mx, mz) or 1.0
        normal = (mx / length, 0.0, mz / length)
        out.extend(_quads_to_triangles(material, normal, (
            (cx * r, 0.0, cz * r), (nx * r, 0.0, nz * r),
            (nx * r, h, nz * r), (cx * r, h, cz * r))))
    for y, normal, flip in ((h, (0.0, 1.0, 0.0), False),
                            (0.0, (0.0, -1.0, 0.0), True)):
        centre = (0.0, y, 0.0)
        for i in range(n):
            cx, cz = ring[i]
            nx, nz = ring[(i + 1) % n]
            a = (cx * r, y, cz * r)
            b = (nx * r, y, nz * r)
            out.append(Triangle(material, normal,
                                (centre, b, a) if flip else (centre, a, b)))
    return out


def quad_triangles(size: Sequence[float], material: int) -> List[Triangle]:
    """A flat rectangle in the XY plane facing +Z.

    Single-sided on purpose. Nothing in either backend enables back-face
    culling, so a quad seen from behind is still drawn - just shaded as a back
    face, which bottoms out at `RenderSettings.ambient` rather than at black. A
    doubled quad would be geometry bought to solve a problem that does not
    exist, and if culling is ever turned on this comment is where to start.
    """
    w, h = (float(v) for v in size)
    return _quads_to_triangles(material, (0.0, 0.0, 1.0),
                               ((0.0, 0.0, 0.0), (w, 0.0, 0.0),
                                (w, h, 0.0), (0.0, h, 0.0)))


#: shape -> the fields it needs and what "valid" means for each. One table, read
#: by both the generator and the lint, so a rule cannot hold in one and not the
#: other.
_PRIMITIVE_FIELDS: Dict[str, Tuple[Tuple[str, int], ...]] = {
    "box": (("size", 3),),
    "cylinder": (("radius", 1), ("height", 1)),
    "quad": (("size", 2),),
}


def _positive_numbers(value: Any, count: int) -> Optional[List[float]]:
    """`count` finite positive numbers, or None with no exception raised."""
    raw = value if count > 1 else [value]
    if not isinstance(raw, (list, tuple)) or len(raw) != count:
        return None
    out: List[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number) or number <= 0.0:
            return None
        out.append(number)
    return out


def primitive_dimension_problem(spec: Mapping[str, Any]) -> Optional[str]:
    """Why this primitive cannot be meshed, in words, or None if it can.

    Separate from meshing so the *lint* can say it, at build time, naming the
    record - rather than the builder raising a traceback halfway through a
    world. A zero-sized box is a model that silently does not appear, which is
    the failure ASSET_SCOPE 3 exists to move to build time.
    """
    shape = spec.get("shape")
    if shape not in PRIMITIVE_SHAPES:
        return None                    # `unknown_primitive_shape` says this
    for field, count in _PRIMITIVE_FIELDS[shape]:
        if _positive_numbers(spec.get(field), count) is None:
            return ("{0!r} primitive needs {1!r} to be {2} finite number{3} "
                    "greater than zero; found {4!r}".format(
                        shape, field, count, "" if count == 1 else "s",
                        spec.get(field)))
    return None


def primitive_triangles(spec: Mapping[str, Any]) -> List[Triangle]:
    """Geometry for one `Model.primitive`. Assumes the lint already passed."""
    problem = primitive_dimension_problem(spec)
    if problem:
        raise ValueError(
            "{0}; the lint (`invalid_primitive_dimensions`) should have "
            "caught this before the mesher saw it".format(problem))
    shape = spec.get("shape")
    material = int(spec.get("material", 1))
    if shape == "box":
        return box_triangles(_positive_numbers(spec.get("size"), 3), material)
    if shape == "cylinder":
        (radius,) = _positive_numbers(spec.get("radius"), 1)
        (height,) = _positive_numbers(spec.get("height"), 1)
        return cylinder_triangles(radius, height, material)
    if shape == "quad":
        return quad_triangles(_positive_numbers(spec.get("size"), 2), material)
    raise ValueError(
        "primitive shape {0!r} is not one of {1}; the lint "
        "(`unknown_primitive_shape`) should have caught this before the "
        "mesher saw it".format(shape, list(PRIMITIVE_SHAPES)))


# ---------------------------------------------------------------------------
# Voxel models, through the mesher that already exists
# ---------------------------------------------------------------------------

def voxel_triangles(model: VoxModel, model_ref: str, *,
                    voxel_size: float = VOXEL_SIZE_M) -> List[Triangle]:
    """A `.vox` model greedy-meshed by `StructureMesher`.

    A model is a structure with no break-state, so `LiveStructure` is
    constructed without one and `mesh_all` sees an intact grid. Nothing here
    re-implements meshing, and that is the point.
    """
    data = to_structure(model, model_ref)
    mesher = StructureMesher(LiveStructure(data), voxel_size=voxel_size)
    mesher.mesh_all()
    out: List[Triangle] = []
    for chunk in mesher.chunks.values():
        for quad in chunk.quads:
            # Quads are already in structure-local metres; the placement
            # origin is *not* applied - a library model has no placement.
            out.extend(_quads_to_triangles(quad.material, quad.normal,
                                           quad.corners))
    return out


# ---------------------------------------------------------------------------
# Anchoring and packing
# ---------------------------------------------------------------------------

def anchor(triangles: Sequence[Triangle]) -> Tuple[List[Triangle], AABB]:
    """Centre on X and Z, sit on y = 0. Returns the moved geometry and bounds.

    Taken from the meshed extent rather than from the authored grid, so
    `to_structure`'s padding to a legal cube - which produces no faces - cannot
    move a model. Getting that backwards would offset every voxel model by half
    its padding, silently and only for some of them.
    """
    points = [c for tri in triangles for c in tri.corners]
    if not points:
        return [], AABB((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    lo = (min(p[0] for p in points), min(p[1] for p in points),
          min(p[2] for p in points))
    hi = (max(p[0] for p in points), max(p[1] for p in points),
          max(p[2] for p in points))
    shift = (-(lo[0] + hi[0]) * 0.5, -lo[1], -(lo[2] + hi[2]) * 0.5)

    def moved(p: Vec3) -> Vec3:
        return (p[0] + shift[0], p[1] + shift[1], p[2] + shift[2])

    out = [Triangle(t.material, t.normal,
                    (moved(t.corners[0]), moved(t.corners[1]),
                     moved(t.corners[2])))
           for t in triangles]
    bounds = AABB((lo[0] + shift[0], lo[1] + shift[1], lo[2] + shift[2]),
                  (hi[0] + shift[0], hi[1] + shift[1], hi[2] + shift[2]))
    return out, bounds


def tint_of(material: int,
            palette: Sequence[Sequence[int]] = ()) -> Rgb:
    """A material index as unit RGB.

    `palette` is a `.vox` file's own `RGBA` table, where index *i* is entry
    *i - 1*. Absent - an untouched MagicaVoxel default, or a primitive, which
    has no palette at all - the engine's `DEFAULT_PALETTE` answers instead, so
    a model and a structure with the same material id look the same.
    """
    from ..render.raster import DEFAULT_PALETTE
    index = int(material) - 1
    if palette and 0 <= index < len(palette):
        entry = palette[index]
        # RGB only: nothing downstream has an alpha channel to put it in, and
        # a partly transparent prop is a renderer feature this scope refuses.
        return (entry[0] / 255.0, entry[1] / 255.0, entry[2] / 255.0)
    fallback = DEFAULT_PALETTE[int(material) % len(DEFAULT_PALETTE)]
    return (fallback[0] / 255.0, fallback[1] / 255.0, fallback[2] / 255.0)


def pack(triangles: Sequence[Triangle],
         palette: Sequence[Sequence[int]] = ()) -> bytes:
    """Triangles as `position, normal, tint` - nine floats a vertex."""
    out = bytearray()
    cache: Dict[int, Rgb] = {}
    for tri in triangles:
        colour = cache.get(tri.material)
        if colour is None:
            colour = cache[tri.material] = tint_of(tri.material, palette)
        for corner in tri.corners:
            out.extend(struct.pack("9f", *corner, *tri.normal, *colour))
    return bytes(out)


# ---------------------------------------------------------------------------
# The two entry points
# ---------------------------------------------------------------------------

def mesh_primitive(model_ref: str, spec: Mapping[str, Any]) -> ModelMesh:
    triangles, bounds = anchor(primitive_triangles(spec))
    return ModelMesh(model_ref=model_ref, kind=MODEL_PRIMITIVE,
                     vertices=pack(triangles), bounds=bounds)


def mesh_voxel_file(model_ref: str, path: str, *, model_index: int = 0,
                    voxel_size: float = VOXEL_SIZE_M) -> ModelMesh:
    models = read_vox(path)
    if model_index >= len(models):
        raise VoxError("{0}: model index {1} requested but the file has "
                       "{2}".format(path, model_index, len(models)))
    model = models[model_index]
    triangles, bounds = anchor(
        voxel_triangles(model, model_ref, voxel_size=voxel_size))
    return ModelMesh(model_ref=model_ref, kind=MODEL_VOXEL,
                     vertices=pack(triangles, model.palette), bounds=bounds)
