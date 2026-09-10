"""`models.lobster_lib` - one meshed copy of every `Model`, shared by the cells.

    magic   b"LOBSTLIB"            8 bytes
    u32     header length (little-endian)
    header  JSON, utf-8
    blobs   concatenated vertex payloads, addressed by {offset, length}

Deliberately the same shape as `lobster.bundle`, and for the same reasons: a
header that can be read without the payload, a checksum over the payload, and a
version the reader refuses rather than guesses at.

**Why this is not in the cell bundle.** A structure bakes *into* its cell, which
is right - a gatehouse is one object in one place and its break-state is
per-instance. Props and items are the opposite: fifty barrels in a town are
fifty *placements* of one model, and baking the mesh fifty times would multiply
a cell's bytes by its decoration (ASSET_SCOPE 2).

**What a model is here.** A packed triangle stream in the layout the GL backend
already draws - `position, normal, tint`, nine floats a vertex,
`MODEL_VERTEX_STRIDE` bytes. Not quads, not voxels, not palette indices:
whatever a model *was*, by the time it is in here it is the one representation
everything downstream already understands. That is the whole point of meshing
both kinds at build time (ASSET_SCOPE 1).

Three things follow from the library being *shared*, and each is a decision
rather than an accident:

* **Tints are resolved, and unlit.** A cell bundle bakes its lightmap into its
  vertex tints, which is free and correct because the value cannot change during
  a residency. A shared model has no cell, so it carries its material colour and
  nothing else; the lighting of a *placement* belongs to the cell it stands in.
* **Geometry is model-local and anchored.** Centred on X and Z, sitting on
  y = 0. A placement's rotation is then a rotation of the thing rather than a
  swing around its corner, which is what an author means by "turn the crate".
* **It is derived, and D1 applies unchanged.** Reproducible from content, never
  authoritative, safe to delete. There is no write path at runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

from .constants import (LIBRARY_FORMAT, LIBRARY_FORMAT_VERSION,
                        MODEL_VERTEX_STRIDE)
from .geometry import AABB

LIBRARY_MAGIC = b"LOBSTLIB"
_HEADER_LEN = struct.Struct("<I")


class ModelLibraryError(Exception):
    """A library that cannot be read, or a model that is not in one.

    Always names the file and the model, because the symptom of getting this
    wrong is a thing that does not appear, and "nothing appeared" names nobody.
    """


@dataclass(frozen=True)
class ModelMesh:
    """One model's geometry, in model-local metres."""

    model_ref: str
    #: `MODEL_VOXEL` or `MODEL_PRIMITIVE`. Carried so a report can say which
    #: kind produced which cost without re-reading the content package.
    kind: str
    #: packed triangles: position, normal, tint - nine floats a vertex
    vertices: bytes = b""
    bounds: AABB = dc_field(
        default_factory=lambda: AABB((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))

    def vertex_count(self) -> int:
        return len(self.vertices) // MODEL_VERTEX_STRIDE

    def triangle_count(self) -> int:
        return self.vertex_count() // 3

    def nbytes(self) -> int:
        return len(self.vertices)

    def is_empty(self) -> bool:
        return not self.vertices

    def bound_radius(self) -> float:
        """A sphere about the model origin that contains the whole model.

        Measured from the *origin* and not from the centre of the bounds,
        because that origin is where a placement puts the model and where a
        rotation turns it - so this radius is the same whichever way the thing
        is facing, which a bounds-centre radius would not be.
        """
        return max(
            (x * x + y * y + z * z) ** 0.5
            for x in (self.bounds.minimum[0], self.bounds.maximum[0])
            for y in (self.bounds.minimum[1], self.bounds.maximum[1])
            for z in (self.bounds.minimum[2], self.bounds.maximum[2]))


@dataclass(frozen=True)
class ModelLibrary:
    """Every distinct model, meshed once. Immutable; there is no setter."""

    models: Dict[str, ModelMesh] = dc_field(default_factory=dict)
    provenance: Dict[str, Any] = dc_field(default_factory=dict)
    source_path: Optional[str] = None

    def model(self, model_ref: str) -> ModelMesh:
        mesh = self.models.get(model_ref)
        if mesh is None:
            raise ModelLibraryError(
                "no model {0!r} in the library ({1}); it holds {2}. Either the "
                "record was added after the last build or the library is "
                "stale - rebuild it.".format(
                    model_ref, self.source_path or "<in memory>",
                    self.model_refs() or "nothing"))
        return mesh

    def model_refs(self) -> List[str]:
        return sorted(self.models)

    def nbytes(self) -> int:
        return sum(m.nbytes() for m in self.models.values())

    def triangle_count(self) -> int:
        return sum(m.triangle_count() for m in self.models.values())

    def report(self) -> Dict[str, Any]:
        """What the build report and the CLI say about a library."""
        return {
            "models": len(self.models),
            "triangles": self.triangle_count(),
            "bytes": self.nbytes(),
            "by_model": [{"model_ref": ref, "kind": self.models[ref].kind,
                          "triangles": self.models[ref].triangle_count(),
                          "bytes": self.models[ref].nbytes()}
                         for ref in self.model_refs()],
        }


def read_library_header(path: str) -> Dict[str, Any]:
    """Header only - no vertex payload read.

    Sizes, kinds and bounds are all header data, so "what would this cost" is
    answerable without paying it.
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(len(LIBRARY_MAGIC))
            if magic != LIBRARY_MAGIC:
                raise ModelLibraryError(
                    "{0}: not a {1} (bad magic {2!r})".format(
                        path, LIBRARY_FORMAT, magic))
            raw_len = f.read(_HEADER_LEN.size)
            if len(raw_len) != _HEADER_LEN.size:
                raise ModelLibraryError(
                    "{0}: truncated before the header length".format(path))
            (length,) = _HEADER_LEN.unpack(raw_len)
            raw = f.read(length)
            if len(raw) != length:
                raise ModelLibraryError("{0}: truncated header".format(path))
    except OSError as e:
        raise ModelLibraryError(
            "{0}: cannot be read ({1})".format(path, e.strerror)) from e
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ModelLibraryError(
            "{0}: unreadable header ({1})".format(path, e)) from e
    if header.get("format") != LIBRARY_FORMAT:
        raise ModelLibraryError("{0}: header declares format {1!r}".format(
            path, header.get("format")))
    version = header.get("format_version")
    if version != LIBRARY_FORMAT_VERSION:
        raise ModelLibraryError(
            "{0}: library format version {1}, this build reads {2}. Rebuild "
            "the world - hot reload is not supported in v1 (Scope 4.5).".format(
                path, version, LIBRARY_FORMAT_VERSION))
    header["_path"] = path
    return header


def read_library(path: str, *, verify: bool = True) -> ModelLibrary:
    header = read_library_header(path)
    with open(path, "rb") as f:
        f.seek(len(LIBRARY_MAGIC))
        (length,) = _HEADER_LEN.unpack(f.read(_HEADER_LEN.size))
        f.seek(length, os.SEEK_CUR)
        payload = f.read()

    if verify and header.get("blob_sha256"):
        actual = hashlib.sha256(payload).hexdigest()
        if actual != header["blob_sha256"]:
            raise ModelLibraryError(
                "{0}: payload checksum mismatch - the library is corrupt or "
                "was truncated in transit. Rebuild it.".format(path))

    models: Dict[str, ModelMesh] = {}
    for raw in header.get("models") or ():
        model_ref = raw.get("model_ref")
        if not model_ref:
            raise ModelLibraryError(
                "{0}: a model entry declares no model_ref".format(path))
        ref = raw.get("vertices") or {}
        start = int(ref.get("offset", 0))
        end = start + int(ref.get("length", 0))
        if end > len(payload):
            raise ModelLibraryError(
                "{0}, model {1!r}: vertices at {2}..{3} run past the payload "
                "({4} bytes)".format(path, model_ref, start, end, len(payload)))
        vertices = payload[start:end]
        # Two statements about one thing - the header's count and the blob's
        # size - and a reader that trusted only the first would hand out half a
        # model. Checked in this order so each is reachable on its own: a count
        # that is not whole triangles can never match a length either, and
        # checking length first would make the triangle rule untestable.
        declared = int(raw.get("vertex_count", 0))
        if declared % 3:
            raise ModelLibraryError(
                "{0}, model {1!r}: {2} vertices is not whole triangles".format(
                    path, model_ref, declared))
        if len(vertices) != declared * MODEL_VERTEX_STRIDE:
            raise ModelLibraryError(
                "{0}, model {1!r}: declares {2} vertices ({3} bytes) but its "
                "blob is {4} bytes".format(
                    path, model_ref, declared,
                    declared * MODEL_VERTEX_STRIDE, len(vertices)))
        bounds = raw.get("bounds") or {}
        models[model_ref] = ModelMesh(
            model_ref=model_ref, kind=raw.get("kind", ""), vertices=vertices,
            bounds=AABB(tuple(bounds.get("min", (0.0, 0.0, 0.0))),
                        tuple(bounds.get("max", (0.0, 0.0, 0.0)))))

    return ModelLibrary(models=models,
                        provenance=dict(header.get("provenance") or {}),
                        source_path=path)
