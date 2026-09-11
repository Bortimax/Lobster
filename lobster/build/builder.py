"""`lobster-build` (Scope 4.5).

> **Build step:** `lobster-build` consumes a world manifest [...] plus per-cell
> `.vox` files, produces one `.lobster_cell` bundle per Location - baked terrain
> mesh, baked navmesh, baked lightmap, undamaged structure definitions, prop and
> sound metadata. Any mod-supplied addition to any of this [...] goes through
> the same operation-log/merge/quarantine path as any other package content; the
> build step must not special-case it into a bypass channel.

The order matters, and it is the order Scope 10 specifies:

1. resolve the content stack (no save layer - a playthrough's damage is not a
   build input);
2. lint, and refuse to bake a broken world;
3. mesh terrain, derive its collider;
4. bake the navmesh, attaching each connection's spawn point as a portal;
5. load the structures - **then** infer `navmesh_load_bearing` per micro-chunk
   against the navmesh that was just baked;
6. bake the lightmap;
7. write the bundle, with provenance naming every record it came from.

Step 5 after step 4 is not an implementation convenience: the inference *is*
"does this chunk intersect a navmesh polygon", so the navmesh has to exist
first.

Nothing here writes an Octopus record and nothing writes save data. The build
produces derived artifacts and a report; if a build has to change a record to
succeed, that is a content bug and the lint says so.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..budgets import Budget, BudgetViolation
from ..bundle import CellBundle, PropPlacement
from ..constants import (BUNDLE_SUFFIX, EXTERIOR_CELL_SIZE_M,
                         LIBRARY_FILENAME, MAX_MODEL_LIBRARY_BYTES)
from ..geometry import Transform, Vec3
from ..navmesh import LoadBearingTable, Navmesh
from ..octopus_bridge import content_view, resolve_item_location
from ..structures import StructureError, StructureVoxelData
from ..terrain import Terrain
from .bundle_writer import write_bundle
from .library_writer import build_library, write_library
from .lighting import bake_lightmap, lighting_report
from .lint import (check_exterior_terrain_fills_its_cell,
                   errors as lint_errors, format_findings, lint_world)
from .manifest import CellEntry, Manifest, ManifestError, load_manifest
from .navmesh_bake import (bake_navmesh, bake_report, unreachable_portals,
                           BakeSettings)
from .navmesh_inference import (infer_load_bearing, inference_report,
                                validate_overrides)
from .terrain_mesher import (ColumnField, column_field_from_vox,
                             flat_column_field, mesh_terrain, terrain_report)
from .vox import VoxError, read_vox, to_structure


class BuildError(Exception):
    """A build that cannot produce a trustworthy bundle. Names the cell."""


@dataclass
class BuildReport:
    """Everything the build decided, per cell. Printed by the CLI verbatim.

    This is the "failure is visible and attributable" invariant (Scope 13) in
    its build-time form: every finding names a record or a cell, and every
    declared cost is here beside the budget it was checked against.
    """

    manifest_path: Optional[str] = None
    findings: List[Dict[str, Any]] = dc_field(default_factory=list)
    cells: List[Dict[str, Any]] = dc_field(default_factory=list)
    written: List[str] = dc_field(default_factory=list)
    #: what `models.lobster_lib` cost, per model and in total. Empty on a build
    #: that never got as far as meshing.
    library: Dict[str, Any] = dc_field(default_factory=dict)

    def errors(self) -> List[Dict[str, Any]]:
        return lint_errors(self.findings)

    def ok(self) -> bool:
        return not self.errors()

    def to_dict(self) -> Dict[str, Any]:
        return {"manifest": self.manifest_path, "ok": self.ok(),
                "findings": list(self.findings),
                "errors": self.errors(),
                "library": dict(self.library),
                "cells": list(self.cells), "written": list(self.written)}


def _portals_for(view: Any, cell_id: str) -> Dict[str, Vec3]:
    """Where each outgoing connection's door is, in this cell's space.

    Scope 4: "the connection owns the spawn point". The spawn transform on this
    cell's connection to X is where an agent arrives *in X*, so the door in
    *this* cell is the spawn point on X's connection back here. Getting this
    backwards would bake every portal in the wrong cell, and the navmesh would
    quietly disagree with the connection graph about every door.
    """
    portals: Dict[str, Vec3] = {}
    record = view.record(cell_id)
    for conn in (record or {}).get("connections") or ():
        target = conn.get("target_location_id") if isinstance(conn, dict) else conn
        if not target:
            continue
        neighbour = view.record(target)
        for back in (neighbour or {}).get("connections") or ():
            if not isinstance(back, dict):
                continue
            if back.get("target_location_id") != cell_id:
                continue
            if back.get("spawn_transform"):
                portals[target] = Transform.from_dict(
                    back["spawn_transform"]).position
    return portals


def _terrain_for(manifest: Manifest, cell: CellEntry) -> Tuple[Terrain, ColumnField]:
    if cell.terrain_vox:
        path = manifest.vox_path(cell.terrain_vox)
        try:
            models = read_vox(path)
        except VoxError as e:
            raise BuildError("cell {0!r}: {1}".format(cell.location_id, e)) from e
        field = column_field_from_vox(models[0], side=cell.terrain_side)
    else:
        side = cell.terrain_side or int(EXTERIOR_CELL_SIZE_M
                                        / cell.navmesh.voxel_size)
        field = flat_column_field(side, height=cell.terrain_height)
    terrain = mesh_terrain(cell.location_id, field,
                           voxel_size=cell.navmesh.voxel_size)
    return terrain, field


def _structures_for(manifest: Manifest,
                    cell: CellEntry) -> List[StructureVoxelData]:
    out: List[StructureVoxelData] = []
    for entry in cell.structures:
        if not entry.vox:
            raise BuildError(
                "cell {0!r}, structure {1!r}: no `vox` file declared; a "
                "structure with no authored grid has nothing to bake".format(
                    cell.location_id, entry.structure_id))
        path = manifest.vox_path(entry.vox)
        try:
            models = read_vox(path)
            if entry.model_index >= len(models):
                raise VoxError(
                    "{0}: model index {1} requested, file has {2}".format(
                        path, entry.model_index, len(models)))
            out.append(to_structure(models[entry.model_index],
                                    entry.structure_id, origin=entry.origin,
                                    chunk_size=entry.chunk_size))
        except (VoxError, StructureError) as e:
            raise BuildError("cell {0!r}: {1}".format(cell.location_id, e)) from e
    return out


def build_cell(view: Any, manifest: Manifest, cell: CellEntry, *,
               out_dir: str, write: bool = True) -> Dict[str, Any]:
    """Bake one Location into a `.lobster_cell`. Returns the report entry."""
    location_record = view.record(cell.location_id)
    budget = Budget.declared(cell.location_id, location_record)

    terrain, field = _terrain_for(manifest, cell)
    portals = _portals_for(view, cell.location_id)
    # Structures first: the navmesh is baked *around* them, not merely
    # inspected for what a later destruction might affect (review L2).
    structures = _structures_for(manifest, cell)
    navmesh = bake_navmesh(cell.location_id, field, settings=cell.navmesh,
                           portals=portals, structures=structures)

    tables: Dict[str, LoadBearingTable] = {}
    inference: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = list(unreachable_portals(navmesh, portals))
    for entry, structure in zip(cell.structures, structures):
        table = infer_load_bearing(navmesh, structure,
                                   overrides=entry.navmesh_overrides)
        tables[structure.structure_id] = table
        inference.append(inference_report(table, structure))
        for issue in validate_overrides(table, structure):
            issue.setdefault("cell_id", cell.location_id)
            findings.append(issue)
    for f in findings:
        f.setdefault("cell_id", cell.location_id)

    lightmap = bake_lightmap(cell.location_id, field, sun=cell.sun,
                             voxel_size=cell.navmesh.voxel_size)

    bundle = CellBundle(
        cell_id=cell.location_id, terrain=terrain, navmesh=navmesh,
        structures=tuple(structures), load_bearing=tables,
        props=tuple(cell.props), lightmap=lightmap.data,
        lightmap_dims=lightmap.dims,
        lightmap_voxel_size=lightmap.voxel_size,
        provenance={
            "records": sorted({cell.location_id}
                              | {s.structure_id for s in structures}),
            "manifest": os.path.basename(manifest.source_path or "<inline>"),
            "manifest_sha256": _manifest_hash(cell),
        })

    # declared budgets, checked before anything is written (L6)
    budget_findings: List[Dict[str, Any]] = []
    for metric, value in (("max_structure_voxels", bundle.structure_voxel_count()),
                          ("max_micro_chunks", bundle.micro_chunk_count()),
                          ("max_drawable_placements",
                           drawable_placements(view, bundle)),
                          ("max_bytes", sum(bundle.nbytes().values()))):
        try:
            budget.check(metric, value, record_id=cell.location_id)
        except BudgetViolation as violation:
            budget_findings.append({"code": "over_budget",
                                    "record_id": violation.record_id,
                                    "cell_id": violation.cell_id,
                                    "detail": str(violation)})
    findings.extend(budget_findings)

    entry_report: Dict[str, Any] = {
        "cell_id": cell.location_id,
        "terrain": terrain_report(terrain, field),
        "navmesh": bake_report(navmesh),
        "lighting": lighting_report(lightmap),
        "structures": inference,
        "declared_costs": bundle.nbytes(),
        "structure_voxels": bundle.structure_voxel_count(),
        "micro_chunks": bundle.micro_chunk_count(),
        "budget": budget.to_dict(),
        "findings": findings,
        "path": None,
    }

    if write and not lint_errors(findings):
        path = os.path.join(out_dir, cell.location_id + BUNDLE_SUFFIX)
        write_bundle(bundle, path)
        entry_report["path"] = path
    return entry_report


def check_library_budget(library: Any) -> List[Dict[str, Any]]:
    """The library is one share of the transition peak (D51).

    Checked against the **whole** library rather than against whatever subset a
    ring happens to reference, which is conservative on purpose: if the whole
    thing fits, every subset does, and one exact build-time number beats a
    runtime pool that would need its own ledger to say anything different.

    The finding names the biggest models, because "19 MiB is too much" is not
    actionable and "these four are 14 MiB of it" is.
    """
    total = library.nbytes()
    if total <= MAX_MODEL_LIBRARY_BYTES:
        return []
    worst = sorted(library.models.values(), key=lambda m: -m.nbytes())[:4]
    return [{"code": "model_library_over_budget", "record_id": None,
             "cell_id": None,
             "detail": "the model library meshes to {0} bytes against a "
                       "ceiling of {1} - one share of the transition peak, on "
                       "the same terms as a resident cell. The largest are "
                       "{2}".format(
                           total, MAX_MODEL_LIBRARY_BYTES,
                           ", ".join("{0} ({1} bytes)".format(m.model_ref,
                                                              m.nbytes())
                                     for m in worst))}]


def drawable_placements(view: Any, bundle: CellBundle) -> int:
    """Things in this cell that will draw as a mesh: props **and** items.

    Both, because both feed the same per-frame counter. Counting props alone
    and dividing the frame budget by the ring anyway was the defect D52
    records: a ceiling on one contributor, derived as though it bounded both.

    Something with no `model_ref` is not counted, here or at runtime. It draws
    as an impostor, which is the cheaper path the frame budget does not charge
    for either.
    """
    props = sum(1 for p in bundle.props if p.model_ref)
    items = sum(1 for record in view.records_of_type("Item")
                if record.get("model_ref") and record.get("world_transform")
                and resolve_item_location(record) == bundle.cell_id)
    return props + items


def _manifest_hash(cell: CellEntry) -> str:
    payload = json.dumps(cell.to_dict(), sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def build_world(manifest: Manifest, *, out_dir: str,
                write: bool = True,
                extra_packages: Sequence[str] = ()) -> BuildReport:
    """Lint the world, then bake every cell in it.

    A world that fails lint is not baked at all, rather than baked partially:
    an unenterable Location or a one-way connection is a content error, and
    shipping half a build over it would hide the error behind a runtime symptom
    three weeks later (Scope 15.2).
    """
    report = BuildReport(manifest_path=manifest.source_path)
    packages = list(manifest.package_paths()) + list(extra_packages)
    view = content_view(packages)

    report.findings.extend(lint_world(view, manifest))
    if report.errors():
        return report

    # The shared model library, before any cell. A model that meshes to nothing
    # is a build error the same way a one-way connection is, and the same rule
    # applies: a world that fails is not baked at all rather than baked partly.
    library, library_findings = build_library(view, manifest)
    report.findings.extend(library_findings)
    report.library = library.report()
    report.findings.extend(check_library_budget(library))
    if report.errors():
        return report

    if write:
        os.makedirs(out_dir, exist_ok=True)
        report.written.append(
            write_library(library, os.path.join(out_dir, LIBRARY_FILENAME)))
    for cell in manifest.cells:
        entry = build_cell(view, manifest, cell, out_dir=out_dir, write=write)
        report.cells.append(entry)
        entry["findings"].extend(check_exterior_terrain_fills_its_cell(
            view, manifest, [entry]))
        report.findings.extend(entry["findings"])
        if entry["path"]:
            report.written.append(entry["path"])
    return report


def build_from_file(manifest_path: str, *, out_dir: str,
                    write: bool = True) -> BuildReport:
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as e:
        raise BuildError(str(e)) from e
    return build_world(manifest, out_dir=out_dir, write=write)
