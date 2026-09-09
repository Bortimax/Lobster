"""The `.lobster_cell` bundle - the sole runtime geometry artifact per Location
(Scope 4.5).

    magic   b"LOBSTER\\x01"      8 bytes
    u32     header length (little-endian)
    header  JSON, utf-8
    blobs   concatenated binary payloads, addressed by {offset, length} in the
            header

What is in it: derived geometry that has no record representation - the baked
terrain mesh and collider, the baked navmesh, the baked lightmap, the authored
structure voxel grids, the inferred load-bearing tables, decorative props.

What is deliberately *not* in it: anything that is an Octopus record or field.
Spawn transforms, zone shapes, sound sources, budgets and break-state are read
from the resolved record layer at load time, never from here. See DECISIONS.md
D1 and D7 - a bundle that carried its own copy of a mod-layerable list would be
the "bypass channel" Scope 4.5 forbids, whether or not anyone wrote to it.

The bundle is therefore a *pure function of its inputs* and safe to delete: it
holds no state a playthrough can change. There is no write path at runtime.
`read_header` exists so an unloaded cell can still be reasoned about - the world
tick in Scope 6.5 needs structure origins and chunk sizes to resolve a blast,
and those are header-sized, not payload-sized.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import (BUNDLE_FORMAT, BUNDLE_FORMAT_VERSION, MICRO_CHUNK_VOXELS,
                        TERRAIN_VOXEL_SIZE_M, VOXEL_SIZE_M)
from .geometry import AABB, Transform
from .navmesh import LoadBearingTable, Navmesh
from .structures import StructureVoxelData
from .terrain import Terrain, TerrainCollider, TerrainMesh

MAGIC = b"LOBSTER\x01"
_HEADER_LEN = struct.Struct("<I")


class BundleError(Exception):
    """A malformed or unreadable bundle. Always names the file and the cell."""


# ---------------------------------------------------------------------------
# In-memory bundle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PropPlacement:
    """Decoration with no gameplay identity - a barrel that is only a barrel.

    Anything the player can pick up, open or be told about is an Octopus `Item`
    record placed through the ordinary path and reported with
    `on_item_placed`; it does not live here.
    """

    prop_id: str
    model_ref: str
    transform: Transform

    def to_dict(self) -> Dict[str, Any]:
        return {"prop_id": self.prop_id, "model_ref": self.model_ref,
                "transform": self.transform.to_dict()}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PropPlacement":
        return cls(prop_id=raw["prop_id"], model_ref=raw.get("model_ref", ""),
                   transform=Transform.from_dict(raw.get("transform")))


@dataclass(frozen=True)
class CellBundle:
    """One Location's baked geometry. Immutable; there is no setter anywhere."""

    cell_id: str
    terrain: Optional[Terrain] = None
    navmesh: Optional[Navmesh] = None
    structures: Tuple[StructureVoxelData, ...] = ()
    load_bearing: Dict[str, LoadBearingTable] = dc_field(default_factory=dict)
    props: Tuple[PropPlacement, ...] = ()
    lightmap: bytes = b""
    lightmap_dims: Tuple[int, int, int] = (0, 0, 0)
    #: metres per lightmap sample. Carried explicitly rather than re-derived
    #: from the terrain collider: the two agree today only because both come
    #: from the same column field, and a coincidence is not a format.
    lightmap_voxel_size: float = TERRAIN_VOXEL_SIZE_M
    #: record ids this bundle was baked from, and the manifest hash, so a
    #: failure can name the record as well as the cell (Scope 13 invariant 2).
    provenance: Dict[str, Any] = dc_field(default_factory=dict)
    source_path: Optional[str] = None

    def structure(self, structure_id: str) -> StructureVoxelData:
        for s in self.structures:
            if s.structure_id == structure_id:
                return s
        raise BundleError(
            "cell {0!r}: no authored structure {1!r} in the bundle".format(
                self.cell_id, structure_id))

    def structure_ids(self) -> List[str]:
        return [s.structure_id for s in self.structures]

    def table_for(self, structure_id: str) -> LoadBearingTable:
        return self.load_bearing.get(structure_id,
                                     LoadBearingTable(structure_id=structure_id))

    # -- declared costs ------------------------------------------------------
    def structure_voxel_count(self) -> int:
        return sum(s.voxel_count for s in self.structures)

    def micro_chunk_count(self) -> int:
        return sum(s.chunk_count for s in self.structures)

    def nbytes(self) -> Dict[str, int]:
        """Per-pool declared residency cost. The loader charges exactly this."""
        return {
            "terrain": self.terrain.nbytes() if self.terrain else 0,
            "structures": sum(s.nbytes() for s in self.structures),
            "navmesh": self.navmesh.nbytes() if self.navmesh else 0,
            "lightmap": len(self.lightmap),
            "metadata": sum(len(p.prop_id) + 64 for p in self.props) + 256,
        }


# ---------------------------------------------------------------------------
# Header <-> bundle
# ---------------------------------------------------------------------------

def read_header(path: str) -> Dict[str, Any]:
    """Header only - no voxel payload read, no mesh decoded.

    This is what lets Scope 6.5's world tick resolve a blast against structures
    in a cell nobody is standing in: origins, grid sizes and chunk sizes are all
    header data, and none of them need the material payload.
    """
    with open(path, "rb") as f:
        magic = f.read(len(MAGIC))
        if magic != MAGIC:
            raise BundleError(
                "{0}: not a {1} bundle (bad magic {2!r})".format(
                    path, BUNDLE_FORMAT, magic))
        raw_len = f.read(_HEADER_LEN.size)
        if len(raw_len) != _HEADER_LEN.size:
            raise BundleError("{0}: truncated before the header length".format(path))
        (length,) = _HEADER_LEN.unpack(raw_len)
        raw = f.read(length)
        if len(raw) != length:
            raise BundleError("{0}: truncated header".format(path))
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BundleError("{0}: unreadable header ({1})".format(path, e)) from e
    if header.get("format") != BUNDLE_FORMAT:
        raise BundleError("{0}: header declares format {1!r}".format(
            path, header.get("format")))
    version = header.get("format_version")
    if version != BUNDLE_FORMAT_VERSION:
        raise BundleError(
            "{0}: bundle format version {1}, this build reads {2}. Rebuild the "
            "cell - hot reload is not supported in v1 (Scope 4.5).".format(
                path, version, BUNDLE_FORMAT_VERSION))
    header["_path"] = path
    return header


def structure_headers(path: str) -> List[Dict[str, Any]]:
    """Just the structure placement headers. Cheap enough for an unloaded cell."""
    return list(read_header(path).get("structures") or ())


def read_bundle(path: str, *, verify: bool = True) -> CellBundle:
    header = read_header(path)
    cell_id = header.get("cell_id")
    if not cell_id:
        raise BundleError("{0}: header declares no cell_id".format(path))
    with open(path, "rb") as f:
        f.seek(len(MAGIC))
        (length,) = _HEADER_LEN.unpack(f.read(_HEADER_LEN.size))
        f.seek(length, os.SEEK_CUR)
        payload = f.read()

    if verify and header.get("blob_sha256"):
        actual = hashlib.sha256(payload).hexdigest()
        if actual != header["blob_sha256"]:
            raise BundleError(
                "cell {0!r} ({1}): payload checksum mismatch - the bundle is "
                "corrupt or was truncated in transit. Rebuild it.".format(
                    cell_id, path))

    def blob(ref: Optional[Dict[str, int]]) -> bytes:
        if not ref:
            return b""
        start = int(ref["offset"])
        end = start + int(ref["length"])
        if end > len(payload):
            raise BundleError(
                "cell {0!r} ({1}): blob at {2}..{3} runs past the payload "
                "({4} bytes)".format(cell_id, path, start, end, len(payload)))
        return payload[start:end]

    terrain: Optional[Terrain] = None
    raw_terrain = header.get("terrain")
    if raw_terrain:
        mesh_head = raw_terrain["mesh"]
        vbytes = blob(raw_terrain.get("vertices"))
        ibytes = blob(raw_terrain.get("indices"))
        hbytes = blob(raw_terrain.get("heights"))
        col_head = raw_terrain["collider"]
        bounds = mesh_head.get("bounds")
        mesh = TerrainMesh(
            cell_id=cell_id,
            vertices=struct.unpack("<{0}f".format(len(vbytes) // 4), vbytes),
            indices=struct.unpack("<{0}I".format(len(ibytes) // 4), ibytes),
            material_slices=tuple(tuple(s) for s in mesh_head.get("material_slices", ())),
            bounds=AABB(tuple(bounds["min"]), tuple(bounds["max"])) if bounds else None)
        collider = TerrainCollider(
            cell_id=cell_id, resolution=int(col_head["resolution"]),
            size_m=float(col_head["size_m"]),
            heights=struct.unpack("<{0}f".format(len(hbytes) // 4), hbytes),
            origin=tuple(col_head.get("origin", (0.0, 0.0, 0.0))))
        terrain = Terrain(cell_id=cell_id, mesh=mesh, collider=collider)

    navmesh = Navmesh.from_dict(header["navmesh"]) if header.get("navmesh") else None

    structures: List[StructureVoxelData] = []
    tables: Dict[str, LoadBearingTable] = {}
    for raw in header.get("structures") or ():
        materials = blob(raw.get("material_ids"))
        expected = int(raw["grid_size"]) ** 3
        if len(materials) != expected:
            raise BundleError(
                "cell {0!r}, structure {1!r}: material payload is {2} bytes, "
                "grid_size {3} needs {4}".format(cell_id, raw["structure_id"],
                                                 len(materials),
                                                 raw["grid_size"], expected))
        structures.append(StructureVoxelData(
            structure_id=raw["structure_id"], grid_size=int(raw["grid_size"]),
            material_ids=materials,
            origin=Transform.from_dict(raw.get("origin")),
            chunk_size=int(raw.get("chunk_size", MICRO_CHUNK_VOXELS)),
            empty_material=int(raw.get("empty_material", 0))))
        if raw.get("load_bearing"):
            tables[raw["structure_id"]] = LoadBearingTable.from_dict(raw["load_bearing"])

    lightmap = b""
    dims = (0, 0, 0)
    lightmap_voxel_size = TERRAIN_VOXEL_SIZE_M
    if header.get("lightmap"):
        lightmap = blob(header["lightmap"].get("data"))
        dims = tuple(header["lightmap"].get("dims", (0, 0, 0)))
        lightmap_voxel_size = float(header["lightmap"].get(
            "voxel_size", TERRAIN_VOXEL_SIZE_M))

    return CellBundle(
        cell_id=cell_id, terrain=terrain, navmesh=navmesh,
        structures=tuple(structures), load_bearing=tables,
        props=tuple(PropPlacement.from_dict(p) for p in header.get("props") or ()),
        lightmap=lightmap, lightmap_dims=dims,
        lightmap_voxel_size=lightmap_voxel_size,
        provenance=dict(header.get("provenance") or {}), source_path=path)
