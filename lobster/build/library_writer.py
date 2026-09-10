"""Building and writing `models.lobster_lib`. Build step only.

Beside the reader's package, not in it, for the reason `bundle_writer` is:
nothing in `lobster/` opens a file for writing, and a test asserts it. A library
is a pure function of its authored inputs, so writing one is a build action and
never a save (L5, D1).

**Every declared `Model` is meshed, not only the referenced ones.** Meshing on
demand would make the artifact depend on which cells happen to place what, so
adding a prop to one cell could change another cell's build - and the linter
already reports assets nobody names. A model no one places costs bytes in a
derived file that is safe to delete.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Tuple

from ..constants import (LIBRARY_FORMAT, LIBRARY_FORMAT_VERSION,
                         MODEL_PRIMITIVE)
from ..model_library import LIBRARY_MAGIC, ModelLibrary, ModelMesh, _HEADER_LEN
from .lint import finding, model_kind
from .manifest import Manifest
from .model_mesher import mesh_primitive, mesh_voxel_file
from .vox import VoxError


def build_library(view: Any, manifest: Manifest) -> Tuple[ModelLibrary,
                                                          List[Dict[str, Any]]]:
    """Mesh every `Model` in the content. Returns the library and its findings.

    Findings rather than exceptions, so a broken model names its record and
    joins the same report every other build failure is in. The one exception is
    a `.vox` that cannot be *parsed*, which `VoxError` already reports with the
    file and the byte offset - re-wrapped here so it also names the record.
    """
    models: Dict[str, ModelMesh] = {}
    findings: List[Dict[str, Any]] = []

    for record in view.records_of_type("Model"):
        model_id = record["id"]
        kind = model_kind(record)
        if kind is None:
            continue                   # `check_models` already said so
        try:
            if kind == MODEL_PRIMITIVE:
                mesh = mesh_primitive(model_id, record.get("primitive") or {})
            else:
                entry = manifest.model(model_id)
                if entry is None:
                    continue           # `model_ref_unresolved` already said so
                mesh = mesh_voxel_file(model_id, manifest.vox_path(entry.vox))
        except (VoxError, ValueError) as e:
            findings.append(finding(
                "model_meshing_failed", str(e), record_id=model_id))
            continue

        # `Model` is `accountable=True` in Octopus and carries `cost_estimate`
        # (ASSET_SCOPE 6: "Octopus's own budget hook and should be honoured
        # rather than re-derived"). Honouring it here means *reporting the
        # truth against it*: Lobster measures the mesh exactly and cannot write
        # the record back, so a declared estimate the geometry has outgrown is
        # a content fact worth surfacing. A warning, not an error - an out of
        # date estimate is dead weight in a report, not a broken build.
        estimate = record.get("cost_estimate") or 0
        if estimate and mesh.nbytes() > estimate:
            findings.append(finding(
                "model_cost_estimate_low",
                "declares cost_estimate {0} and meshes to {1} bytes. Octopus "
                "budgets against the declared number, so it is the one that "
                "is wrong".format(estimate, mesh.nbytes()),
                record_id=model_id))

        if mesh.is_empty():
            findings.append(finding(
                "model_meshes_to_nothing",
                "meshed to no triangles at all, so anything placing it draws "
                "nothing. A {0} model with no solid voxels looks exactly like "
                "a missing one at runtime.".format(kind),
                record_id=model_id))
            continue
        models[model_id] = mesh

    provenance = {
        "records": sorted(models),
        "manifest": os.path.basename(manifest.source_path or "<inline>"),
        "manifest_sha256": _models_hash(manifest),
    }
    return ModelLibrary(models=models, provenance=provenance), findings


def _models_hash(manifest: Manifest) -> str:
    payload = json.dumps([m.to_dict() for m in manifest.models],
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def write_library(library: ModelLibrary, path: str) -> str:
    """Serialise a library. Build-step only; nothing at runtime calls this."""
    blobs: List[bytes] = []
    offset = 0
    entries: List[Dict[str, Any]] = []

    for model_ref in library.model_refs():
        mesh = library.models[model_ref]
        entries.append({
            "model_ref": model_ref,
            "kind": mesh.kind,
            "vertex_count": mesh.vertex_count(),
            "triangle_count": mesh.triangle_count(),
            "bounds": {"min": list(mesh.bounds.minimum),
                       "max": list(mesh.bounds.maximum)},
            "vertices": {"offset": offset, "length": len(mesh.vertices)},
        })
        blobs.append(mesh.vertices)
        offset += len(mesh.vertices)

    payload = b"".join(blobs)
    header: Dict[str, Any] = {
        "format": LIBRARY_FORMAT,
        "format_version": LIBRARY_FORMAT_VERSION,
        "provenance": dict(library.provenance),
        "declared_costs": {"vertices": len(payload)},
        "models": entries,
        "blob_sha256": hashlib.sha256(payload).hexdigest(),
    }
    encoded = json.dumps(header, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as f:
        f.write(LIBRARY_MAGIC)
        f.write(_HEADER_LEN.pack(len(encoded)))
        f.write(encoded)
        f.write(payload)
    return path
