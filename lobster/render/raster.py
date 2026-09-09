"""Software rasteriser: a draw list in, pixels out.

Z-buffered, flat-shaded, triangles only. It consumes exactly the things Lobster
was already producing and nothing was reading:

* `TerrainMesh.vertices` / `.indices` / `.material_slices` - the build-time
  greedy mesh;
* `StructureMesher` chunk quads - the runtime greedy mesh, break-state already
  applied, so a destroyed chunk is simply not in the picture;
* `Skeleton.capsule_for` / `bone_matrices` - the pose, drawn as camera-facing
  capsule impostors;
* the baked `Lightmap` - sampled per triangle through `ambient_at`, which is
  Scope 3's "structures sample ambient at their position" and had no caller
  before this.

Flat shading with a hard-coded palette is the honest level of ambition for an
offline viewer. Scope 1 asks for "small texture atlases **or vertex-coloured
voxels**"; this is the second, and the material index a voxel already carries is
the colour index. Distance fog is here too, from the same section - it is three
lines and it is what the Scope names instead of LOD popping.

No policy: nothing here decides what to draw. `lobster.visibility` did that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Tuple)

from ..camera import Camera
from ..geometry import Vec3, add, cross, distance, dot, normalize, scale, sub
from ..visibility import (DrawList, ENTITY, PROP, STRUCTURE, TERRAIN,
                          build_draw_list)

Colour = Tuple[int, int, int]

#: Palette index -> colour. Index 0 is empty space and never drawn. Deliberately
#: a small, fixed, readable set: a viewer for confirming geometry wants
#: distinguishable materials, not a colour-accurate one.
DEFAULT_PALETTE: Tuple[Colour, ...] = (
    (0, 0, 0),          # 0 - empty, never drawn
    (168, 152, 128),    # 1 - stone
    (122, 148, 92),     # 2 - grass
    (140, 116, 84),     # 3 - earth
    (96, 104, 120),     # 4 - slate
    (176, 168, 144),    # 5 - plaster
    (110, 86, 60),      # 6 - timber
    (150, 150, 158),    # 7 - metal
)

ENTITY_COLOUR: Colour = (196, 128, 112)
PROP_COLOUR: Colour = (150, 122, 90)


@dataclass(frozen=True)
class RenderSettings:
    width: int = 640
    height: int = 360
    #: Scope 1: "distance fog instead of LOD popping".
    fog_colour: Colour = (150, 164, 182)
    fog_start_m: float = 60.0
    fog_end_m: float = 280.0
    #: direction light comes *from*, for flat shading
    sun: Vec3 = (0.4, 0.85, 0.35)
    ambient: float = 0.35
    background: Colour = (150, 164, 182)
    palette: Tuple[Colour, ...] = DEFAULT_PALETTE
    draw_entities: bool = True

    def aspect(self) -> float:
        return self.width / float(self.height)


class Framebuffer:
    """RGB colour + depth. Plain lists; this is a CPU viewer."""

    def __init__(self, width: int, height: int, background: Colour) -> None:
        self.width = width
        self.height = height
        self.colour = bytearray(background * (width * height))
        self.depth = [math.inf] * (width * height)
        self.pixels_written = 0

    def pixel(self, x: int, y: int) -> Colour:
        i = (y * self.width + x) * 3
        return (self.colour[i], self.colour[i + 1], self.colour[i + 2])

    def put(self, x: int, y: int, depth: float, colour: Colour) -> bool:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return False
        index = y * self.width + x
        if depth >= self.depth[index]:
            return False
        self.depth[index] = depth
        offset = index * 3
        self.colour[offset] = colour[0]
        self.colour[offset + 1] = colour[1]
        self.colour[offset + 2] = colour[2]
        self.pixels_written += 1
        return True

    def coverage(self) -> float:
        """Fraction of pixels something was drawn into. A test can assert on
        this without caring what the picture looks like."""
        drawn = sum(1 for d in self.depth if d != math.inf)
        return drawn / float(self.width * self.height)


def _shade(base: Colour, normal: Vec3, settings: RenderSettings,
           light: float) -> Colour:
    lambert = max(0.0, dot(normalize(normal), normalize(settings.sun)))
    level = settings.ambient + (1.0 - settings.ambient) * lambert
    level *= light
    return (min(255, int(base[0] * level)),
            min(255, int(base[1] * level)),
            min(255, int(base[2] * level)))


def _fogged(colour: Colour, depth: float, settings: RenderSettings) -> Colour:
    if depth <= settings.fog_start_m:
        return colour
    span = max(1e-6, settings.fog_end_m - settings.fog_start_m)
    t = min(1.0, (depth - settings.fog_start_m) / span)
    return (int(colour[0] + (settings.fog_colour[0] - colour[0]) * t),
            int(colour[1] + (settings.fog_colour[1] - colour[1]) * t),
            int(colour[2] + (settings.fog_colour[2] - colour[2]) * t))


def _clip_near(view_points: Sequence[Vec3], near: float) -> List[Vec3]:
    """Sutherland-Hodgman clip of a polygon against the near plane, in view
    space where the plane is just `z >= near`.

    This is not optional, and the first render proved it. Dropping any triangle
    with a vertex behind the camera sounds like a fair trade until you notice
    that terrain is greedy-meshed into a handful of enormous quads and the
    camera is standing *on* one of them: at least one corner is behind you
    almost always, so the ground silently never draws.
    """
    out: List[Vec3] = []
    count = len(view_points)
    for i in range(count):
        current = view_points[i]
        following = view_points[(i + 1) % count]
        current_in = current[2] >= near
        following_in = following[2] >= near
        if current_in:
            out.append(current)
        if current_in != following_in:
            span = following[2] - current[2]
            if abs(span) < 1e-12:
                continue
            t = (near - current[2]) / span
            out.append((current[0] + (following[0] - current[0]) * t,
                        current[1] + (following[1] - current[1]) * t,
                        near))
    return out


def _fill_triangle(frame: Framebuffer, camera: Camera, world: Sequence[Vec3],
                   colour: Colour, settings: RenderSettings) -> None:
    """Clip against the near plane, project, and scanline-fill, z-buffered."""
    clipped = _clip_near([camera.to_view(p) for p in world], camera.near)
    if len(clipped) < 3:
        return
    projected_all = [camera.project_view(p, frame.width, frame.height)
                     for p in clipped]
    # a clip can turn one triangle into a quad; fan-triangulate it
    for corner in range(1, len(projected_all) - 1):
        _fill_projected(frame, projected_all[0], projected_all[corner],
                        projected_all[corner + 1], colour, settings)


def _fill_projected(frame: Framebuffer, p0, p1, p2, colour: Colour,
                    settings: RenderSettings) -> None:
    (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = p0, p1, p2

    min_x = max(0, int(math.floor(min(x0, x1, x2))))
    max_x = min(frame.width - 1, int(math.ceil(max(x0, x1, x2))))
    min_y = max(0, int(math.floor(min(y0, y1, y2))))
    max_y = min(frame.height - 1, int(math.ceil(max(y0, y1, y2))))
    if min_x > max_x or min_y > max_y:
        return

    area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(area) < 1e-9:
        return
    inv_area = 1.0 / area

    for py in range(min_y, max_y + 1):
        sy = py + 0.5
        for px in range(min_x, max_x + 1):
            sx = px + 0.5
            w0 = ((x1 - sx) * (y2 - sy) - (x2 - sx) * (y1 - sy)) * inv_area
            w1 = ((x2 - sx) * (y0 - sy) - (x0 - sx) * (y2 - sy)) * inv_area
            w2 = 1.0 - w0 - w1
            if w0 < 0.0 or w1 < 0.0 or w2 < 0.0:
                continue
            depth = w0 * z0 + w1 * z1 + w2 * z2
            frame.put(px, py, depth, _fogged(colour, depth, settings))


def _triangle_normal(a: Vec3, b: Vec3, c: Vec3) -> Vec3:
    return normalize(cross(sub(b, a), sub(c, a)))


def _light_at(cell: Any, point: Vec3) -> float:
    """The baked lightmap, sampled at a position (Scope 3).

    `ResidentCell.ambient_at` is the runtime reader; a cell without one (a
    hand-made fixture) renders lit rather than black.
    """
    sampler = getattr(cell, "ambient_at", None)
    if sampler is None:
        return 1.0
    return max(0.0, min(1.0, sampler(point)))


# ---------------------------------------------------------------------------
# Per-kind drawing
# ---------------------------------------------------------------------------

def _draw_terrain(frame: Framebuffer, camera: Camera, cell: Any,
                  placement: Any, settings: RenderSettings) -> int:
    terrain = getattr(cell, "terrain", None)
    if terrain is None or not terrain.mesh.indices:
        return 0
    mesh = terrain.mesh
    verts = mesh.vertices
    material_of: Dict[int, int] = {}
    for material, start, count in mesh.material_slices:
        for i in range(start, start + count):
            material_of[i] = material

    drawn = 0
    for i in range(0, len(mesh.indices) - 2, 3):
        tri = []
        for k in range(3):
            v = mesh.indices[i + k] * 3
            tri.append(placement.apply((verts[v], verts[v + 1], verts[v + 2])))
        material = material_of.get(i, 1)
        base = settings.palette[material % len(settings.palette)]
        centroid = tuple(sum(p[axis] for p in tri) / 3.0 for axis in range(3))
        colour = _shade(base, _triangle_normal(*tri), settings,
                        _light_at(cell, centroid))
        _fill_triangle(frame, camera, tri, colour, settings)
        drawn += 1
    return drawn


def _draw_structure(frame: Framebuffer, camera: Camera, cell: Any, live: Any,
                    placement: Any, settings: RenderSettings) -> int:
    from ..structure_mesher import StructureMesher
    mesher = StructureMesher(live)
    mesher.mesh_all()
    origin = live.voxel_data.origin
    drawn = 0
    for mesh in mesher.chunks.values():
        for quad in mesh.quads:
            corners = [placement.apply(origin.apply(c)) for c in quad.corners]
            base = settings.palette[quad.material % len(settings.palette)]
            centroid = tuple(sum(p[axis] for p in corners) / 4.0
                             for axis in range(3))
            colour = _shade(base, quad.normal, settings,
                            _light_at(cell, centroid))
            _fill_triangle(frame, camera, corners[:3], colour, settings)
            _fill_triangle(frame, camera,
                           [corners[0], corners[2], corners[3]], colour,
                           settings)
            drawn += 2
    return drawn


def _draw_capsule_impostor(frame: Framebuffer, camera: Camera, capsule: Any,
                           colour: Colour, settings: RenderSettings) -> int:
    """A bone as a camera-facing quad along its segment.

    Not a swept sphere - two triangles. A viewer that needs real capsule
    geometry can build it; this is enough to see where somebody is standing and
    which way their arm is pointing, which is what a pose check needs.
    """
    axis = sub(capsule.b, capsule.a)
    to_camera = sub(camera.position, capsule.a)
    side = cross(axis, to_camera)
    if side == (0.0, 0.0, 0.0):
        return 0
    side = scale(normalize(side), capsule.radius)
    corners = [add(capsule.a, side), add(capsule.b, side),
               sub(capsule.b, side), sub(capsule.a, side)]
    normal = normalize(sub(camera.position, capsule.a))
    shaded = _shade(colour, normal, settings, 1.0)
    _fill_triangle(frame, camera, corners[:3], shaded, settings)
    _fill_triangle(frame, camera, [corners[0], corners[2], corners[3]], shaded,
                   settings)
    return 2


def _draw_entity(frame: Framebuffer, camera: Camera, skeleton: Any,
                 settings: RenderSettings) -> int:
    drawn = 0
    for bone in skeleton.region_set.bones:
        drawn += _draw_capsule_impostor(
            frame, camera, skeleton.capsule_for(bone.bone_id), ENTITY_COLOUR,
            settings)
    return drawn


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def render_draw_list(draw_list: DrawList, cells_by_id: Dict[str, Any], *,
                     settings: Optional[RenderSettings] = None
                     ) -> Framebuffer:
    """Rasterise a draw list. The camera and the culling already happened."""
    settings = settings or RenderSettings()
    frame = Framebuffer(settings.width, settings.height, settings.background)
    camera = draw_list.camera

    for item in draw_list.items:
        cell = cells_by_id.get(item.cell_id)
        if cell is None:
            continue
        if item.kind == TERRAIN:
            _draw_terrain(frame, camera, cell, item.cell_placement, settings)
        elif item.kind == STRUCTURE:
            live = cell.structures.get(item.item_id)
            if live is not None:
                _draw_structure(frame, camera, cell, live, item.cell_placement,
                                settings)
        elif item.kind == ENTITY and settings.draw_entities:
            skeleton = getattr(cell, "skeletons", {}).get(item.item_id)
            if skeleton is not None:
                _draw_entity(frame, camera, skeleton, settings)
        elif item.kind == PROP:
            from ..geometry import Capsule
            base = item.center
            _draw_capsule_impostor(
                frame, camera,
                Capsule(base, (base[0], base[1] + 1.0, base[2]), 0.35),
                PROP_COLOUR, settings)
    return frame


def render_cell(camera: Camera, cell: Any, *,
                settings: Optional[RenderSettings] = None,
                backend: Any = None) -> Framebuffer:
    """Cull and draw one resident cell. An interior, or a quick look."""
    settings = settings or RenderSettings()
    draw_list = build_draw_list(camera.with_aspect(settings.aspect()), [cell])
    return _dispatch(backend, draw_list, {cell.cell_id: cell}, settings)


def render_resident(camera: Camera, manager: Any, view: Any, *,
                    settings: Optional[RenderSettings] = None,
                    backend: Any = None) -> Framebuffer:
    """Draw every resident cell, each at the placement its record declares.

    This is the exterior case, and the one that needs `Location.exterior_grid`:
    without a placement per cell there is no offset to draw a neighbour at, and
    the world stops at the cell boundary. The placements come from the manager,
    which is the only object that knows what is resident - `visibility` still
    invents none of it (DECISIONS.md D21).

    The `view` is forwarded to the culler, so **placed items are drawn**: they
    live in records rather than in the bundle, so without it a dropped sword
    would be pickable and labellable and invisible (D37).
    """
    settings = settings or RenderSettings()
    cells = [manager.resident[cell_id] for cell_id in sorted(manager.resident)]
    draw_list = build_draw_list(camera.with_aspect(settings.aspect()), cells,
                                placements=manager.placements(view),
                                view=view)
    return _dispatch(backend, draw_list,
                     {cell.cell_id: cell for cell in cells}, settings)


def _dispatch(backend: Any, draw_list: DrawList, cells_by_id: Dict[str, Any],
              settings: RenderSettings) -> Framebuffer:
    """Draw through a backend, selecting one when the caller did not."""
    if backend is None:
        from .backend import select_backend
        backend = select_backend()
    return backend.render(draw_list, cells_by_id, settings)
