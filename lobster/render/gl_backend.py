"""The ModernGL backend (RENDER_SCOPE step 4, D22's long-owed answer).

Static geometry is uploaded once per cell residency and drawn from GPU buffers;
`GpuResidency` drives that through the Events (§4). Entities, props and items
are impostors rebuilt per frame from poses, so they go in a small dynamic
buffer and never touch a residency buffer.

**What this deliberately is not.** No window, no input, no frame loop - Lobster
renders, it is not an application (RENDER_SCOPE §7). Rendering goes to an
offscreen framebuffer and stays on the GPU; nothing reads it back unless
somebody asks for a screenshot (§3).

## The shaders stay inside GL ES 3.0

`#version 330 core` here because ModernGL is desktop GL, but nothing in them
needs desktop: no geometry stage, no compute, no texture formats ES lacks. The
whole program is a transform, a lambert term against one directional light, a
per-vertex tint and a fog mix - which is what LOBSTER_SCOPE §1 asks for and all
it asks for. Swapping the two version lines is the ES port.

## Matching the software look, without matching its pixels

The project owner deleted pixel agreement (§2), so this does **not** try to
reproduce the rasteriser bit for bit. It does use the same *model* - the same
`RenderSettings` sun, ambient, fog range and palette - so the two paths look
like the same game rather than two different ones. Where they differ in the
last shade of a triangle edge, that is expected and nobody is asserting
otherwise.
"""

from __future__ import annotations

import math
import struct
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..constants import MODEL_VERTEX_STRIDE
from ..geometry import Vec3, identity4, matrix4, pack_matrix4
# Hoisted out of `_draw_item`, which ran this import once per draw item -
# 2,020 trips through `importlib` for a 400-prop frame, found by profiling
# the per-placement cost the budget is derived from (D53). `visibility`
# does not import the renderer, so there is no cycle to avoid here.
from ..visibility import ENTITY, ITEM, PROP, STRUCTURE, TERRAIN
from .backend import BackendError, BackendInfo, RenderBackend

VERTEX_SHADER = """
#version 330 core

uniform mat4 mvp;
uniform mat4 model;
uniform vec3 sun;
uniform float ambient;

in vec3 in_position;
in vec3 in_normal;
in vec3 in_tint;

out vec3 v_colour;
out float v_depth;

void main() {
    // `model` is where the cell sits (D21). Geometry is uploaded in *cell*
    // coordinates and placed at draw time, so a buffer does not have to be
    // rebuilt when a cell's placement changes and two cells with the same
    // bake could share one.
    vec4 world = model * vec4(in_position, 1.0);
    vec4 clip = mvp * world;
    gl_Position = clip;

    // Flat lambert against one directional light, exactly the model
    // `raster._shade` uses. `v_depth` is view depth, carried separately
    // because the fog curve is metres, not clip space.
    // Placements are rigid (translation + rotation), so the normal needs the
    // rotation but not an inverse-transpose.
    vec3 n = mat3(model) * in_normal;
    float lambert = max(0.0, dot(normalize(n), normalize(sun)));
    float level = ambient + (1.0 - ambient) * lambert;
    v_colour = in_tint * level;
    v_depth = clip.w;
}
"""

INSTANCED_VERTEX_SHADER = """
#version 330 core

uniform mat4 mvp;
uniform vec3 sun;
uniform float ambient;

in vec3 in_position;
in vec3 in_normal;
in vec3 in_tint;

// Per instance, not per vertex: fifty barrels are one mesh and fifty
// transforms (ASSET_SCOPE §4). ES 3.0 has instanced arrays, which is why
// RENDER_SCOPE §1 names instancing as the one exception to its feature
// baseline.
in vec4 in_model_0;
in vec4 in_model_1;
in vec4 in_model_2;
in vec4 in_model_3;
in float in_light;

out vec3 v_colour;
out float v_depth;

void main() {
    // The instance matrix is the *world* placement - the object inside its
    // cell, composed with the cell in the world - so a cell is not a grouping
    // key here the way it is for static geometry.
    mat4 model = mat4(in_model_0, in_model_1, in_model_2, in_model_3);
    vec4 world = model * vec4(in_position, 1.0);
    vec4 clip = mvp * world;
    gl_Position = clip;

    vec3 n = mat3(model) * in_normal;
    float lambert = max(0.0, dot(normalize(n), normalize(sun)));
    float level = ambient + (1.0 - ambient) * lambert;
    // A library tint is unlit by construction (D47): a mesh shared by every
    // cell that places it cannot carry one cell's bake. So the bake arrives
    // here, per instance, sampled where the thing actually stands.
    v_colour = in_tint * level * in_light;
    v_depth = clip.w;
}
"""

FRAGMENT_SHADER = """
#version 330 core

uniform vec3 fog_colour;
uniform float fog_start;
uniform float fog_end;

in vec3 v_colour;
in float v_depth;

out vec4 f_colour;

void main() {
    // Distance fog instead of LOD popping (LOBSTER_SCOPE §1).
    float span = max(1e-6, fog_end - fog_start);
    float t = clamp((v_depth - fog_start) / span, 0.0, 1.0);
    f_colour = vec4(mix(v_colour, fog_colour, t), 1.0);
}
"""

#: bytes per vertex: position, normal, tint - all vec3 floats. Defined in
#: `constants` because the model library packs to the same layout, and one
#: format with two definitions eventually has two formats.
VERTEX_STRIDE = MODEL_VERTEX_STRIDE
VERTEX_FORMAT = "3f 3f 3f"
VERTEX_ATTRIBUTES = ("in_position", "in_normal", "in_tint")

#: per instance: a world matrix as four columns, then the baked light where the
#: instance stands. `/i` is moderngl's divisor-1 marker.
INSTANCE_FORMAT = "4f 4f 4f 4f 1f/i"
INSTANCE_ATTRIBUTES = ("in_model_0", "in_model_1", "in_model_2", "in_model_3",
                       "in_light")
INSTANCE_STRIDE = 17 * 4


class GLFrame:
    """What `render` hands back: a framebuffer that stays on the GPU (§3)."""

    def __init__(self, backend: "ModernGLBackend", width: int,
                 height: int) -> None:
        self.backend = backend
        self.width = width
        self.height = height

    @property
    def texture(self) -> Any:
        return self.backend._colour

    def read_pixels(self) -> bytes:
        """RGB bytes, top-left origin — a **screenshot**, not a frame step.

        This is a real stall: it moves the framebuffer to host memory. It is
        acceptable precisely because nothing on the draw path calls it, and it
        exists so `write_png` and a human eye still work on the GPU path (§3).
        """
        return self.backend._read_pixels()


class CellBuffers:
    """One resident cell's static geometry, live on the GPU."""

    def __init__(self, cell_id: str) -> None:
        self.cell_id = cell_id
        self.terrain: Optional[Tuple[Any, Any, int]] = None
        #: structure_id -> (buffer, vao, vertex count)
        self.structures: Dict[str, Tuple[Any, Any, int]] = {}

    def all_resources(self) -> List[Any]:
        out: List[Any] = []
        if self.terrain is not None:
            out.extend(self.terrain[:2])
        for buf, vao, _n in self.structures.values():
            out.extend((buf, vao))
        return out


class ModernGLBackend(RenderBackend):
    """Draws a culled draw list with OpenGL.

    `context` is injectable so the whole backend can be exercised against
    `RecordingContext` with no GPU present, which is how RENDER_SCOPE §5
    replaces the pixel oracle the owner deleted.
    """

    name = "moderngl"

    def __init__(self, context: Any = None, *, width: int = 640,
                 height: int = 360) -> None:
        self.ctx = context if context is not None else _create_context()
        self.width = width
        self.height = height
        self.cells: Dict[str, CellBuffers] = {}
        #: model_ref -> (buffer, vao, vertex count). Shared by every cell that
        #: places one, which is why it is not inside `CellBuffers`.
        self.models: Dict[str, Tuple[Any, Any, int]] = {}
        #: model_ref -> [instance buffer, instanced vao, capacity in bytes].
        #: One per model and reused across frames: a buffer per *placement*
        #: would be the thing instancing exists to remove, and a buffer per
        #: frame would be a driver allocation every frame for a few hundred
        #: bytes - the mistake `_draw_impostors` already records.
        self._instances: Dict[str, List[Any]] = {}
        #: models a draw list asked for that residency never uploaded. Counted
        #: for the same reason `missing_uploads` is: the symptom is a barrel
        #: that is not there, and silence names nobody.
        self._missing_models: Set[str] = set()
        #: cells a draw list named that were never uploaded. An *instance*
        #: attribute: it used to be a class one, which meant every backend in a
        #: process shared one set and a test could inherit another test's
        #: complaint. Found by the first assertion that looked at teardown.
        self._missing: Set[str] = set()
        #: instances this backend had to pack itself because the draw list did
        #: not carry them. Zero on the normal path; a test asserts that, since
        #: silently repacking would make the kernel look wired when it is not.
        self._repacked = 0
        self._program = self.ctx.program(vertex_shader=VERTEX_SHADER,
                                         fragment_shader=FRAGMENT_SHADER)
        # A second program rather than one with a branch. The static path
        # already works and is tested, and converting terrain and structures to
        # one-instance draws for symmetry would rewrite it for elegance - which
        # is the trade L8 exists to refuse. The fragment stage is shared, so
        # the two differ only in where a placement comes from.
        self._instanced = self.ctx.program(
            vertex_shader=INSTANCED_VERTEX_SHADER,
            fragment_shader=FRAGMENT_SHADER)
        self._colour = self.ctx.texture((width, height), 3)
        self._depth = self.ctx.depth_texture((width, height))
        self._fbo = self.ctx.framebuffer(color_attachments=(self._colour,),
                                         depth_attachment=self._depth)
        #: streamed every frame from poses; impostors are not static geometry.
        #: Allocated once and reused - see `_draw_impostors`.
        self._dynamic: Optional[Any] = None
        self._dynamic_vao: Optional[Any] = None
        self._dynamic_capacity = 0

    @classmethod
    def available(cls) -> BackendInfo:
        from .backend import gl_probe
        renderer, detail = gl_probe()
        if renderer is None:
            return BackendInfo(name=cls.name, available=False, detail=detail)
        return BackendInfo(name=cls.name, available=True, detail=detail)

    # -- residency -----------------------------------------------------------
    def upload_cell(self, cell: Any) -> None:
        """Static geometry to the GPU, once. Idempotent by cell id."""
        if cell.cell_id in self.cells:
            return
        buffers = CellBuffers(cell.cell_id)
        self.cells[cell.cell_id] = buffers

        terrain = _terrain_vertices(cell)
        if terrain:
            buffers.terrain = self._make_mesh(terrain)
        for structure_id in sorted(getattr(cell, "structures", {})):
            self._upload_one_structure(buffers, cell, structure_id)

    def upload_structure(self, cell: Any, structure_id: str,
                         chunk_indices: Sequence[int]) -> None:
        """Refresh one structure after a wall came down.

        `chunk_indices` says which chunks moved; this re-meshes the structure
        rather than the cell, which is the point of the narrower call. Meshing
        a single chunk would be narrower still and is a later optimisation -
        the correctness boundary is that a *cell* is not re-uploaded.
        """
        buffers = self.cells.get(cell.cell_id)
        if buffers is None:
            return
        existing = buffers.structures.pop(structure_id, None)
        if existing is not None:
            _release(existing[0], existing[1])
        self._upload_one_structure(buffers, cell, structure_id)

    # -- shared models -------------------------------------------------------
    def upload_model(self, mesh: Any) -> None:
        """One library model, uploaded once however many cells place it."""
        if mesh.model_ref in self.models or not mesh.vertices:
            return
        self.models[mesh.model_ref] = self._make_mesh(mesh.vertices)

    def release_model(self, model_ref: str) -> None:
        # The instanced VAO holds a reference to the model's vertex buffer, so
        # it has to go with it. Leaving it would mean the next upload of the
        # same ref drew last residency's geometry.
        instance = self._instances.pop(model_ref, None)
        if instance is not None:
            _release(instance[0], instance[1])
        existing = self.models.pop(model_ref, None)
        if existing is not None:
            _release(existing[0], existing[1])

    def release_cell(self, cell_id: str) -> None:
        buffers = self.cells.pop(cell_id, None)
        if buffers is None:
            return
        for resource in buffers.all_resources():
            resource.release()

    def _upload_one_structure(self, buffers: CellBuffers, cell: Any,
                              structure_id: str) -> None:
        live = getattr(cell, "structures", {}).get(structure_id)
        if live is None:
            return
        vertices = _structure_vertices(cell, live)
        if vertices:
            buffers.structures[structure_id] = self._make_mesh(vertices)

    def _make_mesh(self, data: bytes) -> Tuple[Any, Any, int]:
        buf = self.ctx.buffer(data)
        vao = self.ctx.vertex_array(
            self._program, [(buf, VERTEX_FORMAT) + VERTEX_ATTRIBUTES])
        return buf, vao, len(data) // VERTEX_STRIDE

    # -- drawing -------------------------------------------------------------
    def render(self, draw_list: Any, cells_by_id: Dict[str, Any],
               settings: Any, *, library: Any = None) -> GLFrame:
        self._fbo.use()
        # Cleared to the *settings* background, not to black. The software path
        # fills with `settings.background` (the fog colour), and a GPU frame
        # that cleared to black put a black sky above a lit landscape - found
        # by looking at the first PNG, which is what §5 says this method is for.
        self._fbo.clear(*_unit(settings.background), 1.0)
        self.ctx.enable(self.ctx.DEPTH_TEST)

        mvp = _view_projection(draw_list.camera)
        for program in (self._program, self._instanced):
            program["mvp"].write(_pack_matrix(mvp))
            program["sun"].value = tuple(settings.sun)
            program["ambient"].value = float(settings.ambient)
            program["fog_colour"].value = _unit(settings.fog_colour)
            program["fog_start"].value = float(settings.fog_start_m)
            program["fog_end"].value = float(settings.fog_end_m)

        drawn = set()
        impostors = bytearray()
        #: (cell_id, model_ref) -> the draw items in that group, in order. The
        #: bytes usually come from the draw list, already packed by the seam
        #: kernel that culled them (D53); the items are kept so a draw list
        #: built without one can still be packed here.
        groups: Dict[Tuple[str, str], List[Any]] = {}
        placed: Optional[str] = None
        for item in draw_list.items:
            buffers = self.cells.get(item.cell_id)
            if buffers is None:
                # Culled in but never uploaded. Silence here would be a cell
                # that is simply missing from the picture, so it is worth
                # being able to see: `missing_uploads` counts it.
                self._missing.add(item.cell_id)
                continue
            key = (item.cell_id, item.kind, item.item_id)
            if key in drawn:
                continue
            drawn.add(key)
            if item.cell_id != placed:
                # One uniform write per cell, not per draw: the draw list is
                # already grouped, and a redundant upload per triangle batch is
                # the kind of thing that makes a GPU path slower than it looks.
                self._program["model"].write(
                    _pack_matrix(_model_matrix(item.cell_placement)))
                placed = item.cell_id
            self._draw_item(buffers, item, settings, impostors, groups,
                            cells_by_id.get(item.cell_id))

        instances = self._instance_streams(groups, draw_list, cells_by_id)
        if instances:
            self._draw_instanced(instances)
        if impostors:
            # `DrawItem.center` is already world space, so impostors are placed
            # by the culler rather than by a model matrix.
            self._program["model"].write(_pack_matrix(_identity()))
            self._draw_impostors(bytes(impostors))
        return GLFrame(self, self.width, self.height)

    def _draw_item(self, buffers: CellBuffers, item: Any, settings: Any,
                   impostors: bytearray,
                   groups: Dict[Tuple[str, str], List[Any]],
                   cell: Any) -> None:
        if item.kind == TERRAIN and buffers.terrain is not None:
            _, vao, count = buffers.terrain
            vao.render(vertices=count)
        elif item.kind == STRUCTURE:
            mesh = buffers.structures.get(item.item_id)
            if mesh is not None:
                vao = mesh[1]
                vao.render(vertices=mesh[2])
        elif item.kind in (PROP, ITEM) and item.model_ref:
            if item.model_ref not in self.models:
                # Residency never uploaded it. Same fallback the software path
                # takes, and counted here rather than raised - a frame that
                # threw over one absent barrel would take the picture with it.
                self._missing_models.add(item.model_ref)
                impostors.extend(_impostor_vertices(item, settings))
                return
            groups.setdefault((item.cell_id, item.model_ref), []).append(item)
        elif item.kind in (ENTITY, PROP, ITEM):
            impostors.extend(_impostor_vertices(item, settings))

    def _instance_streams(self, groups: Dict[Tuple[str, str], List[Any]],
                          draw_list: Any,
                          cells_by_id: Dict[str, Any]
                          ) -> Dict[str, bytearray]:
        """One byte stream per model, from whoever already has the bytes.

        The culler packs these as a side effect of culling - it has the world
        transform and the light in registers at that moment, and handing them
        back would mean computing them twice (D53). So the normal path is a
        copy, and `_instance_bytes` runs only for a draw list that was built
        without a kernel: a hand-made one in a test, or a caller that culled
        for itself.

        The **length check is the guard**, not an optimisation. A blob is used
        only if it holds exactly one instance per item actually being drawn;
        anything else - a cell that was culled in but never uploaded, a draw
        list edited after the fact - falls back rather than uploading a stream
        that does not line up with the models it is drawn against.
        """
        packed = getattr(draw_list, "instances", None) or {}
        streams: Dict[str, bytearray] = {}
        for key in sorted(groups):
            cell_id, model_ref = key
            items = groups[key]
            stream = streams.setdefault(model_ref, bytearray())
            blob = packed.get(key)
            if blob is not None and len(blob) == len(items) * INSTANCE_STRIDE:
                stream.extend(blob)
                continue
            self._repacked += len(items)
            for item in items:
                stream.extend(_instance_bytes(item, cells_by_id.get(cell_id)))
        return streams

    def _draw_instanced(self, instances: Dict[str, bytearray]) -> None:
        """One draw per distinct model, however many placements it has.

        **Per model, not per model per cell.** ASSET_SCOPE §7 step 5 asked for
        the latter, and the instance matrix carries the cell placement composed
        in, so the cell stopped being a grouping key - one draw for fifty
        barrels spread across three resident cells rather than three. Strictly
        fewer draws, and it protects the same property the scope was after: no
        per-placement buffer, and no draw per barrel. See DECISIONS.md D50.
        """
        for model_ref in sorted(instances):
            data = bytes(instances[model_ref])
            buf, vao, _capacity = self._instance_target(model_ref, len(data))
            buf.write(data)
            vao.render(vertices=self.models[model_ref][2],
                       instances=len(data) // INSTANCE_STRIDE)

    def _instance_target(self, model_ref: str, needed: int) -> List[Any]:
        """This model's instance buffer and VAO, grown and reused.

        Orphan-and-write for the same reason `_draw_impostors` does it: the
        contents change every frame, the shape does not, and telling the driver
        the old contents are dead lets it hand back storage without waiting for
        the last frame to finish reading them.
        """
        entry = self._instances.get(model_ref)
        if entry is None or needed > entry[2]:
            capacity = max(needed, (entry[2] * 2 if entry else 0),
                           8 * INSTANCE_STRIDE)
            if entry is not None:
                _release(entry[0], entry[1])
            buf = self.ctx.buffer(reserve=capacity, dynamic=True)
            vao = self.ctx.vertex_array(self._instanced, [
                (self.models[model_ref][0], VERTEX_FORMAT) + VERTEX_ATTRIBUTES,
                (buf, INSTANCE_FORMAT) + INSTANCE_ATTRIBUTES])
            entry = [buf, vao, capacity]
            self._instances[model_ref] = entry
        else:
            entry[0].orphan()
        return entry

    def _draw_impostors(self, data: bytes) -> None:
        """Stream everything that moves through one reused buffer.

        **Orphan-and-write, not create-and-destroy.** The first version made a
        fresh buffer and VAO every frame, which is a driver allocation per
        frame for data that is a few kilobytes and the same shape every time -
        and it meant "no buffers are created while drawing" could not be
        asserted, because ten frames made ten buffers.

        Orphaning tells the driver the old contents are dead so it can hand
        back new storage without waiting for the last frame to finish reading
        the old - which is the whole point of the idiom, and why this does not
        stall.
        """
        needed = len(data)
        if self._dynamic is None or needed > self._dynamic_capacity:
            # Grown, not resized per frame: doubling means a scene that gains
            # entities settles on a size instead of reallocating every time
            # one more walks into view.
            _release(self._dynamic, self._dynamic_vao)
            self._dynamic_capacity = max(needed, self._dynamic_capacity * 2,
                                         64 * VERTEX_STRIDE)
            self._dynamic = self.ctx.buffer(reserve=self._dynamic_capacity,
                                            dynamic=True)
            self._dynamic_vao = self.ctx.vertex_array(
                self._program,
                [(self._dynamic, VERTEX_FORMAT) + VERTEX_ATTRIBUTES])
        else:
            self._dynamic.orphan()
        self._dynamic.write(data)
        self._dynamic_vao.render(vertices=needed // VERTEX_STRIDE)

    # -- screenshots ---------------------------------------------------------
    def _read_pixels(self) -> bytes:
        """RGB bytes, **top-left origin**, matching `Framebuffer` and `write_png`.

        OpenGL's framebuffer origin is bottom-left and an image file's is
        top-left, so the rows come back reversed and are flipped here. The
        first GPU screenshot was upside down; the geometry was right and the
        convention was not, which is the same shape of bug as the terrain
        normals D22 found by squinting at a PNG.
        """
        raw = self._fbo.read(components=3)
        stride = self.width * 3
        return b"".join(raw[y * stride:(y + 1) * stride]
                        for y in range(self.height - 1, -1, -1))

    def release(self) -> None:
        for cell_id in list(self.cells):
            self.release_cell(cell_id)
        # Models too: they are not inside `CellBuffers` precisely because they
        # outlive any one cell, which also means the cell loop above would have
        # walked straight past them and leaked every one. `release_model` takes
        # the instance buffer with it.
        for model_ref in list(self.models):
            self.release_model(model_ref)
        # No separate loop for `_instances`. An instance entry cannot exist
        # without its model - `_instance_target` needs the model's vertex
        # buffer to build the VAO - and `release_model` takes the pair away
        # together. The loop that used to be here could never run, and a guard
        # nothing can make fire is the D47 shape: it survived mutation because
        # there was no way to make it matter.
        if self._dynamic is not None:
            _release(self._dynamic, self._dynamic_vao)
            self._dynamic = self._dynamic_vao = None
        for resource in (self._fbo, self._colour, self._depth,
                         self._program, self._instanced):
            resource.release()

    def missing_models(self) -> List[str]:
        """Models a draw list asked for that residency never uploaded.

        Empty in a world whose build passed lint. Non-empty means a bundle and
        a library built apart, and the picture has impostors where geometry
        should be.
        """
        return sorted(self._missing_models)

    def missing_uploads(self) -> List[str]:
        """Cells the draw list wanted and no buffer existed for.

        Should always be empty when `GpuResidency` is attached. Non-empty means
        something drew a cell that never entered residency - which shows up as
        a hole in the picture and nothing else.
        """
        return sorted(self._missing)


# ---------------------------------------------------------------------------
# Geometry packing
# ---------------------------------------------------------------------------

def _terrain_vertices(cell: Any) -> bytes:
    terrain = getattr(cell, "terrain", None)
    if terrain is None or not terrain.mesh.indices:
        return b""
    from ..render.raster import DEFAULT_PALETTE
    mesh = terrain.mesh
    verts = mesh.vertices
    material_of: Dict[int, int] = {}
    for material, start, count in mesh.material_slices:
        for i in range(start, start + count):
            material_of[i] = material

    out = bytearray()
    for i in range(0, len(mesh.indices) - 2, 3):
        corners = []
        for k in range(3):
            v = mesh.indices[i + k] * 3
            corners.append((verts[v], verts[v + 1], verts[v + 2]))
        normal = _normal_of(corners)
        base = _unit(DEFAULT_PALETTE[material_of.get(i, 1)
                                     % len(DEFAULT_PALETTE)])
        for corner in corners:
            # The baked lightmap, multiplied into the vertex tint at upload.
            # LOBSTER_SCOPE §1 asks for "baked per-cell lighting", and baking
            # it into static vertex data is what makes it free: no texture, no
            # sampler, nothing the ES subset lacks, and it is correct precisely
            # because the value cannot change during a residency.
            out.extend(struct.pack("9f", *corner, *normal,
                                   *_lit(base, cell, corner)))
    return bytes(out)


def _structure_vertices(cell: Any, live: Any) -> bytes:
    from ..render.raster import DEFAULT_PALETTE
    from ..structure_mesher import StructureMesher
    mesher = StructureMesher(live)
    mesher.mesh_all()
    origin = live.voxel_data.origin

    out = bytearray()
    for mesh in mesher.chunks.values():
        for quad in mesh.quads:
            corners = [origin.apply(c) for c in quad.corners]
            base = _unit(DEFAULT_PALETTE[quad.material % len(DEFAULT_PALETTE)])
            for triangle in ((0, 1, 2), (0, 2, 3)):
                for index in triangle:
                    out.extend(struct.pack(
                        "9f", *corners[index], *quad.normal,
                        *_lit(base, cell, corners[index])))
    return bytes(out)


def _instance_bytes(item: Any, cell: Any) -> bytes:
    """One instance: a world matrix, then the baked light where it stands.

    The matrix is the object's own placement inside its cell composed with the
    cell's placement in the world - the same two steps, in the same order, that
    the software path applies per vertex.

    The light is sampled once per instance rather than once per vertex, which is
    the trade a shared mesh forces: the vertices belong to every cell that
    places this model, so nothing can be baked into them (D47). One sample at
    the placement is what §3 already asks of structures - *"structures sample
    ambient at their position"*.
    """
    from .raster import _light_at
    # Composed as transforms and turned into a matrix once, rather than built
    # as two matrices and multiplied. Both are rigid, so the product is a
    # quaternion multiply and one rotated vector - and this runs once per
    # visible placement per frame, which is what the §6 budget is measured in.
    world = item.cell_placement.compose(item.transform)
    return (_pack_matrix(_model_matrix(world))
            + struct.pack("f", _light_at(cell, world.position)))


def _impostor_vertices(item: Any, settings: Any) -> bytes:
    """A camera-facing quad for a thing that moves.

    Deliberately the same shape the software path uses - two triangles, not a
    swept capsule. A viewer that needs real geometry can build it; this is
    enough to see where somebody is standing.
    """
    from ..render.raster import (ENTITY_COLOUR, ITEM_COLOUR,
                                 ITEM_IMPOSTOR_HEIGHT_M, PROP_COLOUR)
    if item.kind == ENTITY:
        colour, height, half = ENTITY_COLOUR, 1.8, 0.45
    elif item.kind == ITEM:
        colour, height, half = ITEM_COLOUR, ITEM_IMPOSTOR_HEIGHT_M, 0.18
    else:
        colour, height, half = PROP_COLOUR, 1.0, 0.35

    x, y, z = item.center
    tint = _unit(colour)
    normal = (0.0, 0.0, -1.0)
    corners = [(x - half, y, z), (x + half, y, z),
               (x + half, y + height, z), (x - half, y + height, z)]
    out = bytearray()
    for triangle in ((0, 1, 2), (0, 2, 3)):
        for index in triangle:
            out.extend(struct.pack("9f", *corners[index], *normal, *tint))
    return bytes(out)


def _normal_of(corners: Sequence[Vec3]) -> Vec3:
    from ..geometry import cross, normalize, sub
    normal = normalize(cross(sub(corners[1], corners[0]),
                             sub(corners[2], corners[0])))
    return normal if normal != (0.0, 0.0, 0.0) else (0.0, 1.0, 0.0)


def _unit(colour: Sequence[int]) -> Tuple[float, float, float]:
    return (colour[0] / 255.0, colour[1] / 255.0, colour[2] / 255.0)


def _lit(tint: Tuple[float, float, float], cell: Any,
         point: Vec3) -> Tuple[float, float, float]:
    """`tint` scaled by the cell's baked lightmap at a world point.

    Uses `raster._light_at`, so both backends read the same bake through the
    same function - a second sampler would be a second thing to keep correct
    and a second thing to get subtly wrong.
    """
    from .raster import _light_at
    level = _light_at(cell, point)
    return (tint[0] * level, tint[1] * level, tint[2] * level)


# ---------------------------------------------------------------------------
# Matrices
# ---------------------------------------------------------------------------

def _view_projection(camera: Any) -> List[List[float]]:
    """One matrix, from the camera's own basis and frustum.

    Built here rather than added to `Camera` because a matrix is a rendering
    concern: `lobster.camera` answers geometric questions (is this sphere
    visible, where does this point land) and gains nothing from a 4x4.
    """
    right, up, forward = camera.basis()
    eye = camera.position
    tan_x, tan_y = camera.tan_half_fov()
    near, far = camera.near, camera.far

    view = [
        [right[0], right[1], right[2], -_dot(right, eye)],
        [up[0], up[1], up[2], -_dot(up, eye)],
        [forward[0], forward[1], forward[2], -_dot(forward, eye)],
        [0.0, 0.0, 0.0, 1.0],
    ]
    # `Camera.to_view` puts +z *forward*, so the projection negates rather than
    # the view matrix - the same convention `project_view` applies.
    projection = [
        [1.0 / tan_x, 0.0, 0.0, 0.0],
        [0.0, 1.0 / tan_y, 0.0, 0.0],
        [0.0, 0.0, (far + near) / (far - near),
         -2.0 * far * near / (far - near)],
        [0.0, 0.0, 1.0, 0.0],
    ]
    return _matmul(projection, view)


# These moved to `geometry`, where a `Transform` as a 4x4 belongs and where the
# accelerator seam's reference can reach them without importing the renderer.
# Aliased rather than renamed at every call site, the same way `VERTEX_STRIDE`
# is - one definition, and no diff in the drawing code.
_identity = identity4
_model_matrix = matrix4


def _matmul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)]
            for r in range(4)]


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


_pack_matrix = pack_matrix4


def _release(*resources: Any) -> None:
    for resource in resources:
        if resource is not None:
            resource.release()


def _create_context() -> Any:                # pragma: no cover - needs a GPU
    from .backend import _CONTEXT_ATTEMPTS
    try:
        import moderngl
    except Exception as exc:
        raise BackendError(
            "the moderngl backend needs the moderngl package: {0}".format(exc))
    from .backend import prime_gl_probe
    # Reuse a context that already exists rather than making a second one.
    # Two contexts in a process is not two renderers - the newer one takes
    # currency and the older one's buffers stop drawing anywhere. A second
    # `ModernGLBackend` turned a live framebuffer into a blank one exactly that
    # way, and `select_backend()` constructs a backend, so it is not an exotic
    # path (D44).
    try:
        existing = moderngl.get_context()
    except Exception:
        existing = None
    if existing is not None:
        prime_gl_probe(str(existing.info.get("GL_RENDERER", "")) or "unknown",
                       "context already current")
        return existing

    failures = []
    for label, kwargs in _CONTEXT_ATTEMPTS:
        try:
            ctx = moderngl.create_context(**kwargs)
        except Exception as exc:
            failures.append("{0}: {1}".format(label, exc))
            continue
        # This context knows what GL is, so record it. Otherwise the next
        # `probe()` builds a second context and releasing it takes this one
        # with it - a live framebuffer went entirely black that way.
        prime_gl_probe(str(ctx.info.get("GL_RENDERER", "")) or "unknown",
                       "context via {0}".format(label))
        return ctx
    raise BackendError(
        "moderngl imports but no backend gave a context - {0}".format(
            "; ".join(failures)))
