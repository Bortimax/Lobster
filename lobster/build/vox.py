"""MagicaVoxel `.vox` reader (Scope 4.5).

> **Authoring format:** MagicaVoxel `.vox` for structures and terrain chunks.

The format, for reference, since this reads it by hand rather than pulling in a
dependency:

    "VOX " + int32 version
    chunks: id[4] + int32 content_size + int32 children_size + content + children
      MAIN  - container
      SIZE  - int32 x, y, z
      XYZI  - int32 count, then count * (u8 x, u8 y, u8 z, u8 colour_index)
      RGBA  - 256 * (r, g, b, a); palette index i is entry i-1

Two conversions happen here and nowhere else, so that every other module can
assume one convention:

**Axes.** MagicaVoxel is Z-up; Lobster is Y-up (`lobster.geometry`). A voxel at
vox (x, y, z) lands at Lobster (x, z, y).

**Shape.** `.vox` models are arbitrary boxes up to 256 a side;
`StructureVoxelData` requires a cube whose side is a multiple of `chunk_size`
(Scope 6). The reader pads with empty voxels up to the next such cube rather
than rejecting the model - padding is invisible in the mesh, costs one byte per
padded voxel, and rejecting would make every author resize by hand for a
constraint that exists for the micro-chunk index, not for them.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..constants import MICRO_CHUNK_VOXELS
from ..geometry import Transform
from ..structures import StructureVoxelData

_INT = struct.Struct("<i")
MAX_VOX_DIMENSION = 256


class VoxError(Exception):
    """A `.vox` file that cannot be read. Always names the file."""


@dataclass(frozen=True)
class VoxModel:
    """One model out of a `.vox` file, already converted to Y-up."""

    size: Tuple[int, int, int]
    #: (x, y, z) -> palette index, Y-up, empty voxels absent
    voxels: Dict[Tuple[int, int, int], int] = dc_field(default_factory=dict)
    palette: Tuple[Tuple[int, int, int, int], ...] = ()
    source_path: Optional[str] = None

    def max_dimension(self) -> int:
        return max(self.size) if self.size else 0

    def solid_count(self) -> int:
        return len(self.voxels)


def _read_chunk(data: bytes, offset: int) -> Tuple[str, bytes, int, int]:
    if offset + 12 > len(data):
        raise VoxError("truncated chunk header at byte {0}".format(offset))
    chunk_id = data[offset:offset + 4].decode("ascii", "replace")
    (content_size,) = _INT.unpack_from(data, offset + 4)
    (children_size,) = _INT.unpack_from(data, offset + 8)
    start = offset + 12
    end = start + content_size
    if end > len(data):
        raise VoxError("chunk {0!r} claims {1} content bytes but the file ends "
                       "at {2}".format(chunk_id, content_size, len(data)))
    return chunk_id, data[start:end], end, end + children_size


def read_vox(path: str) -> List[VoxModel]:
    """Every model in a `.vox` file, in file order."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise VoxError("{0}: cannot be read ({1})".format(path, e.strerror)) from e
    if len(data) < 8 or data[:4] != b"VOX ":
        raise VoxError("{0}: not a MagicaVoxel .vox file".format(path))

    sizes: List[Tuple[int, int, int]] = []
    models: List[Dict[Tuple[int, int, int], int]] = []
    palette: Tuple[Tuple[int, int, int, int], ...] = ()

    offset = 8
    chunk_id, _content, _end, _next = _read_chunk(data, offset)
    if chunk_id != "MAIN":
        raise VoxError("{0}: expected a MAIN chunk, found {1!r}".format(
            path, chunk_id))
    offset += 12                      # MAIN's content is empty; walk children

    while offset < len(data):
        chunk_id, content, content_end, next_offset = _read_chunk(data, offset)
        if chunk_id == "SIZE":
            x, y, z = struct.unpack_from("<iii", content, 0)
            sizes.append((int(x), int(y), int(z)))
        elif chunk_id == "XYZI":
            (count,) = _INT.unpack_from(content, 0)
            voxels: Dict[Tuple[int, int, int], int] = {}
            for i in range(count):
                vx, vy, vz, colour = content[4 + i * 4: 8 + i * 4]
                # Z-up -> Y-up
                voxels[(vx, vz, vy)] = colour
            models.append(voxels)
        elif chunk_id == "RGBA":
            palette = tuple(
                tuple(content[i * 4:(i + 1) * 4]) for i in range(256))
        offset = max(next_offset, content_end)

    if not models:
        raise VoxError("{0}: contains no XYZI voxel data".format(path))
    out: List[VoxModel] = []
    for index, voxels in enumerate(models):
        raw_size = sizes[index] if index < len(sizes) else (0, 0, 0)
        size = (raw_size[0], raw_size[2], raw_size[1])       # Z-up -> Y-up
        for dimension in size:
            if dimension > MAX_VOX_DIMENSION:
                raise VoxError(
                    "{0}: model {1} is {2} voxels on a side; MagicaVoxel's "
                    "limit is {3}".format(path, index, dimension,
                                          MAX_VOX_DIMENSION))
        out.append(VoxModel(size=size, voxels=voxels, palette=palette,
                            source_path=path))
    return out


def cube_side(model: VoxModel, chunk_size: int = MICRO_CHUNK_VOXELS) -> int:
    """The smallest cube side that fits the model and divides by `chunk_size`."""
    longest = max(model.max_dimension(), chunk_size)
    remainder = longest % chunk_size
    return longest if remainder == 0 else longest + (chunk_size - remainder)


def to_structure(model: VoxModel, structure_id: str, *,
                 origin: Optional[Transform] = None,
                 chunk_size: int = MICRO_CHUNK_VOXELS,
                 empty_material: int = 0) -> StructureVoxelData:
    """A `.vox` model as authored structure data, padded to a legal cube."""
    side = cube_side(model, chunk_size)
    materials = bytearray([empty_material]) * (side ** 3)
    for (x, y, z), colour in model.voxels.items():
        if x >= side or y >= side or z >= side:
            raise VoxError(
                "{0}: voxel ({1}, {2}, {3}) falls outside the padded cube of "
                "{4}".format(model.source_path, x, y, z, side))
        if colour == empty_material:
            # palette index 0 is Lobster's empty marker; a model that actually
            # uses it would produce holes, so it is bumped rather than dropped.
            colour = empty_material + 1
        materials[x + y * side + z * side * side] = colour
    return StructureVoxelData(structure_id=structure_id, grid_size=side,
                              material_ids=bytes(materials),
                              origin=origin or Transform(),
                              chunk_size=chunk_size,
                              empty_material=empty_material)


def load_structure(path: str, structure_id: str, *,
                   origin: Optional[Transform] = None,
                   model_index: int = 0,
                   chunk_size: int = MICRO_CHUNK_VOXELS) -> StructureVoxelData:
    models = read_vox(path)
    if model_index >= len(models):
        raise VoxError("{0}: model index {1} requested but the file has "
                       "{2}".format(path, model_index, len(models)))
    return to_structure(models[model_index], structure_id, origin=origin,
                        chunk_size=chunk_size)


def describe(path: str) -> Dict[str, Any]:
    """What the build report says about a `.vox` input."""
    models = read_vox(path)
    return {
        "path": os.path.basename(path),
        "models": [{"index": i, "size": list(m.size),
                    "solid_voxels": m.solid_count(),
                    "padded_cube_side": cube_side(m)}
                   for i, m in enumerate(models)],
    }
