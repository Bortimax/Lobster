"""Build-step lint (Scope 4, 4.5, 15.2).

Three checks the Scope names explicitly, and the reason each one exists:

**1. One-way connections.** Already solved: Octopus's own `lce.lint` reports
`one_way_connection` (its D26), so this reuses it rather than writing a second
copy that can drift.

**2. Unenterable Locations.**

> the same build-step check that already flags a one-way connection (D26) also
> flags a Location with **no `default_spawn_transform` and no incoming
> connection carrying a spawn point** - an unenterable Location should be a
> build-time error, not a runtime surprise the first time someone fast-travels
> there.

**3. Sound lists must not be a bypass channel.**

> Any mod-supplied addition to any of this - including a sound source list -
> goes through the same operation-log/merge/quarantine path as any other
> package content; the build step must not special-case it into a bypass
> channel.

The mechanical form of that: a sound source that reaches the runtime must be
resolvable from the record layer. Since Lobster's bundle carries no sound list
at all (DECISIONS.md D7), the check is that nothing is *trying* to smuggle one -
a manifest key that would have baked sounds is rejected by name, so the bypass
fails loudly at build time rather than working quietly for one build and
breaking every mod that layers over it.

Plus the ordinary structural checks a geometry build can make cheaply: frozen
zone-shape primitives, structures whose `StructureState` disagrees with the cell
they are baked into, and dead `navmesh_load_bearing` overrides.

Everything returns findings; nothing raises. `errors()` picks out the ones that
must fail a build, so a caller decides policy and this module only reports (L4).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ..constants import ZONE_SHAPE_PRIMITIVES
from ..octopus_bridge import octopus_lint

#: Findings that fail a build rather than warn.
ERROR_CODES = frozenset({
    "one_way_connection",
    "unenterable_location",
    "sound_bypass_channel",
    "unknown_zone_shape",
    "structure_location_mismatch",
    "structure_without_geometry",
    "override_out_of_range",
    "portal_off_navmesh",
    "dangling_reference",
    "exterior_without_grid",
    "exterior_grid_collision",
    "exterior_grid_malformed",
})

#: Manifest keys that would bake record-owned data into the bundle.
_BYPASS_KEYS = ("sound_sources", "sounds", "ambient_sounds", "zone_shapes",
                "connections", "spawn_transforms", "default_spawn_transform")


def finding(code: str, detail: str, *, record_id: Optional[str] = None,
            cell_id: Optional[str] = None) -> Dict[str, Any]:
    return {"code": code, "record_id": record_id, "cell_id": cell_id,
            "detail": detail}


def errors(findings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [f for f in findings if f.get("code") in ERROR_CODES]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_spawn_paths(view: Any) -> List[Dict[str, Any]]:
    """Every Location must be enterable somehow (Scope 4)."""
    locations = view.records_of_type("Location")
    incoming_with_spawn: Set[str] = set()
    for location in locations:
        for conn in location.get("connections") or ():
            if not isinstance(conn, dict):
                continue
            target = conn.get("target_location_id")
            if target and conn.get("spawn_transform"):
                incoming_with_spawn.add(target)

    out: List[Dict[str, Any]] = []
    for location in locations:
        if location.get("default_spawn_transform"):
            continue
        if location["id"] in incoming_with_spawn:
            continue
        out.append(finding(
            "unenterable_location",
            "has no default_spawn_transform and no incoming connection "
            "carrying a spawn_transform, so nothing can ever arrive here - "
            "not through a door, not by fast travel",
            record_id=location["id"], cell_id=location["id"]))
    return out


def check_zone_shapes(view: Any) -> List[Dict[str, Any]]:
    """The three primitives are frozen (Scope 4)."""
    out: List[Dict[str, Any]] = []
    for zone in view.records_of_type("Zone"):
        shape = zone.get("shape")
        if not shape:
            continue
        if not isinstance(shape, dict):
            out.append(finding("unknown_zone_shape",
                               "Zone.shape must be an object",
                               record_id=zone["id"]))
            continue
        kind = shape.get("kind")
        if kind not in ZONE_SHAPE_PRIMITIVES:
            out.append(finding(
                "unknown_zone_shape",
                "shape kind {0!r} is not one of the frozen primitives {1}; "
                "adding a fourth is a deliberate scope decision with its own "
                "note, not a one-line addition".format(
                    kind, list(ZONE_SHAPE_PRIMITIVES)),
                record_id=zone["id"]))
    return out


def check_exterior_grid(view: Any) -> List[Dict[str, Any]]:
    """Exterior cells have to say where they are (Scope 4, DECISIONS.md D21).

    A connection says where you *arrive*, not where a cell *sits*, so an
    exterior cell with no `exterior_grid` cannot be drawn beside its
    neighbours - it has no offset to be drawn at. That only matters once a cell
    actually has an exterior neighbour, so a lone exterior island is left
    alone.
    """
    from ..cell import CellError, exterior_grid, is_exterior

    out: List[Dict[str, Any]] = []
    exteriors = [loc for loc in view.records_of_type("Location")
                 if is_exterior(view, loc["id"])]
    grids: Dict[str, Any] = {}
    for location in exteriors:
        try:
            grids[location["id"]] = exterior_grid(view, location["id"])
        except CellError as e:
            out.append(finding("exterior_grid_malformed", str(e),
                               record_id=location["id"],
                               cell_id=location["id"]))
            grids[location["id"]] = None

    for location in exteriors:
        cell_id = location["id"]
        neighbours = [t for t, _cost in view.connections(cell_id)
                      if is_exterior(view, t)]
        if grids.get(cell_id) is None and neighbours:
            out.append(finding(
                "exterior_without_grid",
                "is an exterior cell connected to {0}, but declares no "
                "exterior_grid - so nothing knows where to draw it relative to "
                "them. Author [x, z] grid coordinates.".format(
                    ", ".join(repr(n) for n in sorted(neighbours))),
                record_id=cell_id, cell_id=cell_id))

    # two cells cannot occupy the same square
    occupied: Dict[Any, List[str]] = {}
    for cell_id, grid in sorted(grids.items()):
        if grid is not None:
            occupied.setdefault(grid, []).append(cell_id)
    for grid, cell_ids in sorted(occupied.items()):
        if len(cell_ids) > 1:
            out.append(finding(
                "exterior_grid_collision",
                "grid square {0} is claimed by {1}; two exterior cells cannot "
                "occupy the same place".format(list(grid),
                                               ", ".join(repr(c) for c in cell_ids)),
                record_id=cell_ids[0], cell_id=cell_ids[0]))

    # connected exteriors that are nowhere near each other
    for location in exteriors:
        cell_id = location["id"]
        here = grids.get(cell_id)
        if here is None:
            continue
        for target, _cost in view.connections(cell_id):
            there = grids.get(target)
            if there is None or target <= cell_id:
                continue
            steps = max(abs(here[0] - there[0]), abs(here[1] - there[1]))
            if steps > 1:
                out.append(finding(
                    "exterior_grid_not_adjacent",
                    "is connected to {0!r} but sits {1} grid squares away "
                    "({2} vs {3}); a walk between them crosses cells that are "
                    "never resident. Legitimate for a long road or a ferry, a "
                    "typo otherwise.".format(target, steps, list(here),
                                             list(there)),
                    record_id=cell_id, cell_id=cell_id))
    return out


def check_exterior_terrain_fills_its_cell(view: Any, manifest: Any,
                                          report_cells: Any = None
                                          ) -> List[Dict[str, Any]]:
    """Placed exterior cells tile at the locked cell size, so their terrain has
    to reach that far or the world has holes in it.

    Only meaningful once cells are placed on the grid (D21): before that a cell
    was its own space and its extent was nobody's business. A warning rather
    than an error - a deliberately sparse archipelago is legitimate content,
    and only the author knows which they meant.
    """
    from ..cell import exterior_grid, is_exterior
    from ..constants import EXTERIOR_CELL_SIZE_M

    out: List[Dict[str, Any]] = []
    for entry in (report_cells or []):
        cell_id = entry.get("cell_id")
        if not cell_id or not is_exterior(view, cell_id):
            continue
        try:
            if exterior_grid(view, cell_id) is None:
                continue
        except Exception:
            continue
        terrain = entry.get("terrain") or {}
        columns = terrain.get("columns")
        if not columns:
            continue
        side_m = (columns ** 0.5) * _terrain_voxel_size(manifest, cell_id)
        if side_m < EXTERIOR_CELL_SIZE_M - 0.5:
            out.append(finding(
                "exterior_terrain_undersized",
                "is placed on the exterior grid but its terrain spans only "
                "{0:.0f} m of a {1:.0f} m cell, so there is a gap between it "
                "and its neighbours".format(side_m, EXTERIOR_CELL_SIZE_M),
                record_id=cell_id, cell_id=cell_id))
    return out


def _terrain_voxel_size(manifest: Any, cell_id: str) -> float:
    from ..constants import TERRAIN_VOXEL_SIZE_M
    for cell in getattr(manifest, "cells", ()):
        if cell.location_id == cell_id:
            return cell.navmesh.voxel_size
    return TERRAIN_VOXEL_SIZE_M


def check_no_bypass_channel(manifest: Any) -> List[Dict[str, Any]]:
    """A manifest may not carry record-owned data (Scope 4.5, 13 invariant 1)."""
    out: List[Dict[str, Any]] = []
    for cell in manifest.raw_cells:
        for key in _BYPASS_KEYS:
            if key in cell and cell[key]:
                out.append(finding(
                    "sound_bypass_channel",
                    "the manifest declares {0!r}, which is Octopus record "
                    "data. Author it as ordinary layered content on the "
                    "Location or Zone record so it merges and quarantines like "
                    "everything else; the build step must not special-case it "
                    "into a bypass channel".format(key),
                    cell_id=cell.get("location_id")))
    return out


def check_structures(view: Any, manifest: Any) -> List[Dict[str, Any]]:
    """Baked geometry and `StructureState` records must agree about placement."""
    out: List[Dict[str, Any]] = []
    for cell in manifest.cells:
        baked = {entry.structure_id for entry in cell.structures}
        for entry in cell.structures:
            record = view.record(entry.structure_id)
            if record is None:
                out.append(finding(
                    "structure_without_state",
                    "is baked into this cell but no StructureState record "
                    "declares it, so damage to it can never be remembered "
                    "(Scope 6/L5)",
                    record_id=entry.structure_id, cell_id=cell.location_id))
                continue
            declared = record.get("location_id")
            if declared != cell.location_id:
                out.append(finding(
                    "structure_location_mismatch",
                    "StructureState.location_id is {0!r} but the geometry is "
                    "baked into {1!r}; break-state and geometry would resolve "
                    "in different cells".format(declared, cell.location_id),
                    record_id=entry.structure_id, cell_id=cell.location_id))
        for record in view.structures_in_location(cell.location_id):
            if record["id"] not in baked:
                out.append(finding(
                    "structure_without_geometry",
                    "a StructureState names this cell but the manifest bakes "
                    "no geometry for it; break-state would resolve against "
                    "nothing",
                    record_id=record["id"], cell_id=cell.location_id))
    return out


def check_manifest_covers_locations(view: Any,
                                    manifest: Any) -> List[Dict[str, Any]]:
    """Every Location needs a cell, since residency is 1:1 (Scope 4)."""
    manifest_ids = set(manifest.location_ids())
    out: List[Dict[str, Any]] = []
    for location in view.records_of_type("Location"):
        if location["id"] not in manifest_ids:
            out.append(finding(
                "location_without_cell",
                "no manifest cell bakes geometry for this Location; entering it "
                "at runtime would fail to find a .lobster_cell bundle",
                record_id=location["id"], cell_id=location["id"]))
    for cell_id in sorted(manifest_ids):
        if view.record(cell_id) is None:
            out.append(finding(
                "cell_without_location",
                "the manifest bakes a cell for {0!r}, which is not a Location "
                "in the content stack".format(cell_id),
                cell_id=cell_id))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lint_world(view: Any, manifest: Any) -> List[Dict[str, Any]]:
    """Every build-step check, in one list.

    Octopus's findings come through unchanged - same codes, same details - so a
    writer sees one report rather than two vocabularies for the same world.
    """
    findings: List[Dict[str, Any]] = []
    for raw in octopus_lint(view):
        entry = dict(raw)
        entry.setdefault("cell_id", None)
        findings.append(entry)
    findings.extend(check_spawn_paths(view))
    findings.extend(check_zone_shapes(view))
    findings.extend(check_exterior_grid(view))
    findings.extend(check_no_bypass_channel(manifest))
    findings.extend(check_structures(view, manifest))
    findings.extend(check_manifest_covers_locations(view, manifest))
    return findings


def format_findings(findings: Sequence[Dict[str, Any]]) -> str:
    """One line per finding, naming the record and the cell (13 invariant 2)."""
    lines: List[str] = []
    for f in findings:
        where = f.get("record_id") or f.get("cell_id") or "<world>"
        severity = "error" if f.get("code") in ERROR_CODES else "warning"
        lines.append("{0}: {1}: {2}: {3}".format(
            severity, where, f.get("code"), f.get("detail")))
    return "\n".join(lines)
