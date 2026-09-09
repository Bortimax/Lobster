"""Writing `.lobster_cell` bundles. Build step only.

This lives in `lobster.build` and not beside the reader on purpose: the runtime
package has no file-write path at all, and a test asserts it. L5 - "Damage that
must be remembered is an Octopus record, not a Lobster save file" - is easiest
to keep true when the runtime physically cannot open a file for writing.

A bundle is a pure function of its authored inputs, so writing one is always a
build action and never a save.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any, Dict, List, Optional

from ..bundle import MAGIC, _HEADER_LEN, CellBundle
from ..constants import BUNDLE_FORMAT, BUNDLE_FORMAT_VERSION


def _blob_ref(offset: int, payload: bytes) -> Dict[str, int]:
    return {"offset": offset, "length": len(payload)}


def write_bundle(bundle: CellBundle, path: str) -> str:
    """Serialise a bundle. Build-step only; nothing at runtime calls this."""
    blobs: List[bytes] = []
    offset = 0

    def push(payload: bytes) -> Dict[str, int]:
        nonlocal offset
        ref = _blob_ref(offset, payload)
        blobs.append(payload)
        offset += len(payload)
        return ref

    header: Dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "cell_id": bundle.cell_id,
        "provenance": dict(bundle.provenance),
        "declared_costs": bundle.nbytes(),
        "structure_voxel_count": bundle.structure_voxel_count(),
        "micro_chunk_count": bundle.micro_chunk_count(),
    }

    if bundle.terrain is not None:
        t = bundle.terrain
        header["terrain"] = {
            "mesh": t.mesh.to_header(),
            "collider": t.collider.to_header(),
            "vertices": push(struct.pack("<{0}f".format(len(t.mesh.vertices)),
                                         *t.mesh.vertices)),
            "indices": push(struct.pack("<{0}I".format(len(t.mesh.indices)),
                                        *t.mesh.indices)),
            "heights": push(struct.pack("<{0}f".format(len(t.collider.heights)),
                                        *t.collider.heights)),
        }

    if bundle.navmesh is not None:
        header["navmesh"] = bundle.navmesh.to_dict()

    header["structures"] = [
        {**s.to_bundle_dict(),
         "material_ids": push(bytes(s.material_ids)),
         "load_bearing": bundle.table_for(s.structure_id).to_dict()}
        for s in bundle.structures
    ]

    header["props"] = [p.to_dict() for p in bundle.props]
    if bundle.lightmap:
        header["lightmap"] = {"dims": list(bundle.lightmap_dims),
                              "voxel_size": bundle.lightmap_voxel_size,
                              "data": push(bundle.lightmap)}

    payload = b"".join(blobs)
    header["blob_sha256"] = hashlib.sha256(payload).hexdigest()
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(_HEADER_LEN.pack(len(encoded)))
        f.write(encoded)
        f.write(payload)
    return path
