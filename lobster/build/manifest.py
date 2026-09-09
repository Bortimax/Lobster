"""The world manifest `lobster-build` consumes (Scope 4.5).

> **Build step:** `lobster-build` consumes a world manifest (cells, connections
> + spawn transforms, zone shapes) plus per-cell `.vox` files, produces one
> `.lobster_cell` bundle per Location.

One thing to be clear about, because it looks like a contradiction: connections,
spawn transforms and zone shapes are **not** authored in this file. They are
Octopus records, and the manifest points at the packages that declare them
(DECISIONS.md D1/D7). What the manifest carries is the half Octopus has no
vocabulary for - which `.vox` file is which cell's terrain, where a structure
stands, the navmesh bake settings, the sun direction. Geometry that no record
can express.

That split is what keeps a mod able to move a spawn point, add a sound source or
re-shape a zone through ordinary layered content, without the build step
becoming a second authority on any of it.

    {
      "format": "lobster-manifest",
      "version": 1,
      "packages": ["packages/world.json"],
      "vox_dir": "art",
      "cells": [
        {
          "location_id": "cell-village",
          "terrain_vox": "village.vox",
          "sun": [0.4, 0.8, 0.45],
          "navmesh": {"agent_height": 1.8, "max_step": 0.6, "max_slope": 1.0},
          "structures": [
            {"structure_id": "keep-gatehouse",
             "vox": "gatehouse.vox",
             "origin": {"position": [12, 0, 30], "rotation": [0,0,0,1]},
             "navmesh_load_bearing_overrides": {"41": false}}
          ],
          "props": [{"prop_id": "barrel-1", "model_ref": "model-barrel",
                     "transform": {"position": [4, 0, 4]}}]
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..bundle import PropPlacement
from ..geometry import Transform, Vec3, vec3
from .lighting import DEFAULT_SUN
from .navmesh_bake import BakeSettings

MANIFEST_FORMAT = "lobster-manifest"
MANIFEST_VERSION = 1


class ManifestError(Exception):
    """A manifest that cannot be trusted. Always names the cell or the field."""


@dataclass(frozen=True)
class StructureEntry:
    """One structure placed in a cell."""

    structure_id: str
    vox: Optional[str] = None
    origin: Transform = dc_field(default_factory=Transform)
    model_index: int = 0
    chunk_size: int = 8
    #: author opt-outs for the inferred `navmesh_load_bearing` flag (Scope 10.2)
    navmesh_overrides: Dict[int, bool] = dc_field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, cell_id: str) -> "StructureEntry":
        structure_id = raw.get("structure_id")
        if not structure_id:
            raise ManifestError(
                "cell {0!r}: a structure entry has no structure_id".format(cell_id))
        overrides_raw = raw.get("navmesh_load_bearing_overrides") or {}
        overrides: Dict[int, bool] = {}
        for key, value in overrides_raw.items():
            try:
                overrides[int(key)] = bool(value)
            except (TypeError, ValueError):
                raise ManifestError(
                    "cell {0!r}, structure {1!r}: navmesh override key {2!r} is "
                    "not a chunk index".format(cell_id, structure_id, key))
        return cls(structure_id=structure_id, vox=raw.get("vox"),
                   origin=Transform.from_dict(raw.get("origin")),
                   model_index=int(raw.get("model_index", 0)),
                   chunk_size=int(raw.get("chunk_size", 8)),
                   navmesh_overrides=overrides)

    def to_dict(self) -> Dict[str, Any]:
        return {"structure_id": self.structure_id, "vox": self.vox,
                "origin": self.origin.to_dict(),
                "model_index": self.model_index, "chunk_size": self.chunk_size,
                "navmesh_load_bearing_overrides":
                    {str(k): v for k, v in sorted(self.navmesh_overrides.items())}}


@dataclass(frozen=True)
class CellEntry:
    """One Location's geometry inputs."""

    location_id: str
    terrain_vox: Optional[str] = None
    terrain_side: Optional[int] = None
    terrain_height: int = 1
    sun: Vec3 = DEFAULT_SUN
    navmesh: BakeSettings = dc_field(default_factory=BakeSettings)
    structures: Tuple[StructureEntry, ...] = ()
    props: Tuple[PropPlacement, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CellEntry":
        location_id = raw.get("location_id")
        if not location_id:
            raise ManifestError("a cell entry has no location_id")
        return cls(
            location_id=location_id,
            terrain_vox=raw.get("terrain_vox"),
            terrain_side=(int(raw["terrain_side"])
                          if raw.get("terrain_side") else None),
            terrain_height=int(raw.get("terrain_height", 1)),
            sun=vec3(raw.get("sun", DEFAULT_SUN), what="sun"),
            navmesh=BakeSettings.from_dict(raw.get("navmesh")),
            structures=tuple(StructureEntry.from_dict(s, cell_id=location_id)
                             for s in raw.get("structures") or ()),
            props=tuple(PropPlacement.from_dict(p)
                        for p in raw.get("props") or ()))

    def to_dict(self) -> Dict[str, Any]:
        return {"location_id": self.location_id,
                "terrain_vox": self.terrain_vox,
                "terrain_side": self.terrain_side,
                "terrain_height": self.terrain_height,
                "sun": list(self.sun),
                "navmesh": self.navmesh.to_dict(),
                "structures": [s.to_dict() for s in self.structures],
                "props": [p.to_dict() for p in self.props]}


@dataclass(frozen=True)
class Manifest:
    """The whole world's geometry inputs."""

    cells: Tuple[CellEntry, ...] = ()
    packages: Tuple[str, ...] = ()
    vox_dir: str = "."
    source_path: Optional[str] = None
    #: the cell entries exactly as authored, keys the loader ignored included.
    #: The bypass-channel lint reads these: a key this loader silently drops is
    #: exactly the one an author believes is doing something (Scope 4.5).
    raw_cells: Tuple[Dict[str, Any], ...] = ()

    def cell(self, location_id: str) -> CellEntry:
        for entry in self.cells:
            if entry.location_id == location_id:
                return entry
        raise ManifestError(
            "no cell {0!r} in the manifest".format(location_id))

    def location_ids(self) -> List[str]:
        return [c.location_id for c in self.cells]

    def resolve(self, relative: str) -> str:
        base = os.path.dirname(self.source_path) if self.source_path else "."
        if os.path.isabs(relative):
            return relative
        return os.path.normpath(os.path.join(base, relative))

    def vox_path(self, name: str) -> str:
        return self.resolve(os.path.join(self.vox_dir, name))

    def package_paths(self) -> List[str]:
        return [self.resolve(p) for p in self.packages]

    def to_dict(self) -> Dict[str, Any]:
        return {"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION,
                "packages": list(self.packages), "vox_dir": self.vox_dir,
                "cells": [c.to_dict() for c in self.cells]}


def manifest_from_dict(raw: Mapping[str, Any], *,
                       source_path: Optional[str] = None) -> Manifest:
    if raw.get("format") != MANIFEST_FORMAT:
        raise ManifestError(
            "{0}: not a {1} (found format {2!r})".format(
                source_path or "<dict>", MANIFEST_FORMAT, raw.get("format")))
    version = raw.get("version", MANIFEST_VERSION)
    if int(version) != MANIFEST_VERSION:
        raise ManifestError(
            "{0}: manifest version {1}, this build reads {2}".format(
                source_path or "<dict>", version, MANIFEST_VERSION))
    cells = tuple(CellEntry.from_dict(c) for c in raw.get("cells") or ())
    seen: Dict[str, int] = {}
    for cell in cells:
        seen[cell.location_id] = seen.get(cell.location_id, 0) + 1
    duplicates = sorted(k for k, v in seen.items() if v > 1)
    if duplicates:
        raise ManifestError(
            "{0}: {1} appears more than once; a Location maps to exactly one "
            "cell (Scope 4)".format(source_path or "<dict>", duplicates))
    return Manifest(cells=cells,
                    packages=tuple(raw.get("packages") or ()),
                    vox_dir=raw.get("vox_dir", "."),
                    source_path=source_path,
                    raw_cells=tuple(dict(c) for c in raw.get("cells") or ()))


def load_manifest(path: str) -> Manifest:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ManifestError("{0}: cannot be read ({1})".format(
            path, e.strerror)) from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        lines = text.splitlines()
        excerpt = lines[e.lineno - 1].rstrip() if 0 < e.lineno <= len(lines) else ""
        raise ManifestError(
            "{0}: invalid JSON at line {1}, column {2}: {3}\n    {4}".format(
                os.path.basename(path), e.lineno, e.colno, e.msg, excerpt)) from e
    return manifest_from_dict(raw, source_path=path)
