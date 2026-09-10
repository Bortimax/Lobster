"""Lobster's command line.

Enough surface to do the things the shell promises, and nothing else:

    lobster build     <manifest> --out DIR      bake .lobster_cell bundles
    lobster lint      <manifest>                content + geometry lint only
    lobster inspect   <bundle>                  dump a bundle header
    lobster load      <cell> --cells DIR ...    load a cell, apply StructureState
    lobster damage    <cell> <structure> N...   destroy chunks, show the ops
    lobster budgets   --cells DIR ...           declared vs. actual, per cell
    lobster render    <cell> --cells DIR --out P  draw a cell to a PNG
    lobster contract                            the Scope 13 surface, verbatim
    lobster test                                the Scope 14 suite

Everything prints JSON unless it prints a lint report, because the output of a
build is data somebody else's tool reads (Scope 12: the authoring tools are a
separate repo, and this is what they bind against).

`--packages` takes Octopus content packages. Lobster ships two of its own -
`packages/lobster_geometry.json` and `packages/lobster_limb_state.json` - and
they are included automatically unless `--no-default-packages` is given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from .budgets import (Budget, BudgetViolation, MemoryLedger,
                      transition_peak_findings)
from .bundle import BundleError, read_bundle, read_header
from .cell import CellError, CellManager
from .constants import (BUNDLE_SUFFIX, EXTERIOR_CELL_SIZE_M,
                        MAX_TRANSITION_PEAK_BYTES, MICRO_CHUNK_VOXELS,
                        SPATIAL_GRID_CELL_M, VOXEL_SIZE_M,
                        ZONE_SHAPE_PRIMITIVES)
from .events import CONTRACT_EVENTS, EventBus, PAYLOAD_TYPES
from .octopus_bridge import (PERMITTED_QUERIES, STRUCTURE_STATE_TYPE,
                             OctopusBridge, content_view,
                             delegates_to_octopus)
from .structure_state import destroy_op
from .tiers import CHEAPEST_TIER, FIDELITY, TIER_KIND, TIERS

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PACKAGES = (
    os.path.join(_REPO_ROOT, "packages", "lobster_geometry.json"),
    os.path.join(_REPO_ROOT, "packages", "lobster_limb_state.json"),
)


def _emit(payload: Any) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _packages(args: argparse.Namespace) -> List[str]:
    packages: List[str] = []
    if not getattr(args, "no_default_packages", False):
        packages.extend(DEFAULT_PACKAGES)
    packages.extend(getattr(args, "packages", None) or ())
    return packages


def _view(args: argparse.Namespace):
    return content_view(_packages(args))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    from .build.builder import BuildError, build_from_file
    from .build.lint import format_findings
    try:
        report = build_from_file(args.manifest, out_dir=args.out,
                                 write=not args.dry_run)
    except BuildError as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2
    if args.json:
        _emit(report.to_dict())
    else:
        if report.findings:
            print(format_findings(report.findings))
        for cell in report.cells:
            print("{0}: {1} triangles, {2} navmesh polys, {3} micro-chunks, "
                  "{4} bytes".format(cell["cell_id"],
                                     cell["terrain"]["triangles"],
                                     cell["navmesh"]["polys"],
                                     cell["micro_chunks"],
                                     sum(cell["declared_costs"].values())))
        print("{0} bundle(s) written to {1}".format(len(report.written),
                                                    args.out))
    return 0 if report.ok() else 1


def cmd_lint(args: argparse.Namespace) -> int:
    from .build.builder import BuildError
    from .build.lint import errors, format_findings, lint_world
    from .build.manifest import ManifestError, load_manifest
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2
    view = content_view(list(manifest.package_paths()) + _packages(args)
                        if args.packages else manifest.package_paths())
    findings = lint_world(view, manifest)
    if args.json:
        _emit({"ok": not errors(findings), "findings": findings})
    else:
        print(format_findings(findings) or "no findings")
    return 1 if errors(findings) else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        header = read_header(args.bundle)
    except BundleError as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2
    header.pop("_path", None)
    for structure in header.get("structures") or ():
        structure.pop("material_ids", None)
    if header.get("navmesh") and not args.full:
        header["navmesh"] = {"cell_id": header["navmesh"]["cell_id"],
                             "polys": len(header["navmesh"].get("polys") or ())}
    if header.get("terrain") and not args.full:
        header["terrain"] = header["terrain"]["mesh"]
    return _emit(header)


def cmd_load(args: argparse.Namespace) -> int:
    """Load a cell, apply StructureState, and report what happened."""
    view = _view(args)
    bus = EventBus()
    manager = CellManager(args.cells, bus=bus)
    try:
        cell = manager.load(view, args.cell)
    except (CellError, BudgetViolation) as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2
    payload = {"cell": cell.report(), "events": bus.dump(),
               "ledger": manager.ledger.report(),
               "quarantined": manager.quarantined()}
    if args.mesh:
        from .structure_mesher import StructureMesher
        meshes = {}
        for structure_id, live in sorted(cell.structures.items()):
            mesher = StructureMesher(live)
            mesher.mesh_all()
            meshes[structure_id] = mesher.report()
        payload["meshes"] = meshes
    return _emit(payload)


def cmd_damage(args: argparse.Namespace) -> int:
    """Destroy micro-chunks and show the operation that remembers it."""
    view = _view(args)
    bus = EventBus()
    manager = CellManager(args.cells, bus=bus)
    try:
        manager.load(view, args.cell)
        result = manager.damage_structure(view, args.cell, args.structure,
                                          args.chunks)
    except (CellError, BudgetViolation) as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2
    manager.pump_navmesh(max_jobs=16)
    return _emit({
        "result": result,
        "octopus_op": destroy_op(args.structure, result["destroyed"]),
        "events": bus.dump(),
        "cell": manager.resident[args.cell].report(),
    })


def cmd_budgets(args: argparse.Namespace) -> int:
    """Declared budget versus what each bundle actually costs."""
    view = _view(args)
    rows: List[Dict[str, Any]] = []
    worst = 0
    for name in sorted(os.listdir(args.cells)):
        if not name.endswith(BUNDLE_SUFFIX):
            continue
        cell_id = name[: -len(BUNDLE_SUFFIX)]
        header = read_header(os.path.join(args.cells, name))
        record = view.record(cell_id)
        try:
            budget = Budget.declared(cell_id, record)
        except BudgetViolation as e:
            rows.append({"cell_id": cell_id, "error": str(e)})
            worst = 1
            continue
        costs = header.get("declared_costs") or {}
        total = sum(costs.values())
        over = []
        for metric, value in (
                ("max_structure_voxels", header.get("structure_voxel_count", 0)),
                ("max_micro_chunks", header.get("micro_chunk_count", 0)),
                ("max_bytes", total)):
            if value > budget.limit_for(metric):
                over.append({"metric": metric, "value": value,
                             "limit": budget.limit_for(metric)})
        if over:
            worst = 1
        rows.append({"cell_id": cell_id, "budget": budget.to_dict(),
                     "declared_costs": costs, "total_bytes": total,
                     "structure_voxels": header.get("structure_voxel_count", 0),
                     "micro_chunks": header.get("micro_chunk_count", 0),
                     "over_budget": over})

    # A per-cell ceiling only means something next to the residency it will be
    # part of: a transition holds the union of both rings, so declared budgets
    # have to compose across it, not just fit individually.
    composed = transition_peak_findings(
        view, actual_bytes={r["cell_id"]: r["total_bytes"] for r in rows})
    if composed:
        worst = 1

    _emit({"cells": rows,
           "over_transition_peak": composed,
           "peak_transition_limit": MAX_TRANSITION_PEAK_BYTES,
           "shell_constants": {
               "exterior_cell_size_m": EXTERIOR_CELL_SIZE_M,
               "voxel_size_m": VOXEL_SIZE_M,
               "micro_chunk_voxels": MICRO_CHUNK_VOXELS,
               "spatial_grid_cell_m": SPATIAL_GRID_CELL_M}})
    return worst


def _point(text: str, what: str) -> Any:
    parts = [p for p in text.replace(",", " ").split() if p]
    if len(parts) != 3:
        raise SystemExit("error: --{0} wants three numbers, got {1!r}".format(
            what, text))
    return tuple(float(p) for p in parts)


def cmd_render(args: argparse.Namespace) -> int:
    """Draw a resident cell to a PNG. Offline; there is no window or loop."""
    from .camera import Camera
    from .render import (BackendError, RenderSettings, render_resident,
                         select_backend, write_png)
    view = _view(args)
    manager = CellManager(args.cells)
    try:
        manager.set_player_cell(view, args.cell)
        backend = select_backend(args.backend)
    except (CellError, BudgetViolation, BackendError) as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2

    settings = RenderSettings(width=args.width, height=args.height)
    eye = _point(args.eye, "eye")
    target = (_point(args.look_at, "look-at") if args.look_at
              else (eye[0] + 1.0, eye[1], eye[2]))
    # the whole resident set, each cell at the placement its record declares -
    # so an exterior draws continuously across its ring (DECISIONS.md D21)
    frame = render_resident(Camera.looking_at(eye, target), manager, view,
                            settings=settings, backend=backend)
    write_png(args.out, frame.width, frame.height, frame.colour)
    return _emit({"cell": args.cell, "out": args.out, "backend": backend.name,
                  "size": [frame.width, frame.height],
                  "coverage": round(frame.coverage(), 4),
                  "pixels_written": frame.pixels_written,
                  "resident": sorted(manager.resident),
                  "placements": {c: list(t.position) for c, t
                                 in manager.placements(view).items()},
                  "camera": {"eye": list(eye), "look_at": list(target)}})


def cmd_conformance(args: argparse.Namespace) -> int:
    """The differential seam suite (DECISIONS.md D28).

    With only the Python kernels present this checks the reference against the
    committed vectors, which is what pins the geometry. When a native module
    exists it is the same call with two implementations.
    """
    from . import conformance as conf
    if args.emit_vectors:
        # Printed, not written: nothing in `lobster/` opens a file for writing
        # (CONTRACT invariant 1). Redirect it -
        #   python -m lobster.cli conformance --emit-vectors > conformance/vectors.json
        sys.stdout.write(conf.dump_vectors())
        return 0

    path = args.vectors or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "conformance", "vectors.json")
    from . import accel
    cases, expected = conf.load_vectors(path)
    if args.impl:
        impl_name, kernels = accel.select(args.impl)
        divergences = conf.compare(cases, expected, conf.run(cases, impl=kernels))
    else:
        impl_name, _ = accel.select()
        divergences = conf.compare(cases, expected, conf.run(cases))
        impl_name = "python"
    _emit({"vectors": path,
           "cases": len(cases),
           "kernels": list(conf.SEAM_KERNELS),
           "authority": conf.AUTHORITY,
           "compared": impl_name,
           "distance_tolerance_m": conf.DISTANCE_TOLERANCE_M,
           "accelerators": accel.available(),
           "kernel_preference": {k: list(v) for k, v
                                 in accel.KERNEL_PREFERENCE.items()},
           "divergences": [d.to_dict() for d in divergences]})
    return 1 if divergences else 0


def cmd_contract(args: argparse.Namespace) -> int:
    """The Scope 13 surface, as data. What Shrimp binds against."""
    import inspect as _inspect
    events = {}
    for name in CONTRACT_EVENTS:
        payload = PAYLOAD_TYPES[name]
        fields = [f for f in payload.__dataclass_fields__ if f != "name"]
        events[name] = {"payload_type": payload.__name__, "fields": fields}
    from .render import probe as probe_backends, selection_report
    from .selection import SELECTION_KINDS, Selection
    from .skeleton import HUMANOID_REGIONS, Skeleton
    return _emit({
        "render_backends": [i.to_dict() for i in probe_backends()],
        "render_backend_selected": selection_report().to_dict(),
        "events": events,
        "permitted_queries": list(PERMITTED_QUERIES),
        "queries_delegating_to_octopus": delegates_to_octopus(),
        "save_record": {
            "type": STRUCTURE_STATE_TYPE,
            "fields": ["id", "location_id", "destroyed_chunks"],
            "merge_policy": "UNION_TOMBSTONED"},
        "tiers": {tier: FIDELITY[tier] for tier in TIERS},
        "tier_kind": TIER_KIND,
        "tier_requiring_no_rig": CHEAPEST_TIER,
        "hit_regions": list(HUMANOID_REGIONS),
        "region_is_nullable":
            "region is null only when the target has no rig; every tier with a "
            "hitbox resolves one of hit_regions (DECISIONS.md D16)",
        "set_pose": str(_inspect.signature(Skeleton.set_pose)),
        "zone_shape_primitives": list(ZONE_SHAPE_PRIMITIVES),
        "selection_kinds": list(SELECTION_KINDS),
        "selection_fields": [f for f in Selection.__dataclass_fields__],
        "invariants": [
            "Geometry is data - no private save format anywhere, including a "
            "mod's sound-source list.",
            "Failure is visible and attributable - over-budget content names "
            "the specific record, the specific cell.",
            "No hidden coupling to a specific game's content."],
    })


def cmd_test(args: argparse.Namespace) -> int:
    """Run the Scope 14 suite."""
    import unittest
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(_REPO_ROOT, "tests"),
                            top_level_dir=_REPO_ROOT)
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    return 0 if runner.run(suite).wasSuccessful() else 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lobster", description="Lobster - the voxel shell.")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_packages(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--packages", nargs="*", default=[],
                       help="Octopus content package files, in load order")
        p.add_argument("--no-default-packages", action="store_true",
                       help="omit Lobster's own schema-extension packages")
        return p

    p = sub.add_parser("build", help="bake .lobster_cell bundles")
    p.add_argument("manifest")
    p.add_argument("--out", default="cells")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_build)

    p = with_packages(sub.add_parser("lint", help="lint a world without baking"))
    p.add_argument("manifest")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("inspect", help="dump a bundle header")
    p.add_argument("bundle")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = with_packages(sub.add_parser("load", help="load a cell and report"))
    p.add_argument("cell")
    p.add_argument("--cells", default="cells")
    p.add_argument("--mesh", action="store_true",
                   help="also mesh every structure and report the cost")
    p.set_defaults(func=cmd_load)

    p = with_packages(sub.add_parser("damage", help="destroy micro-chunks"))
    p.add_argument("cell")
    p.add_argument("structure")
    p.add_argument("chunks", nargs="+", type=int)
    p.add_argument("--cells", default="cells")
    p.set_defaults(func=cmd_damage)

    p = with_packages(sub.add_parser("budgets", help="declared vs. actual cost"))
    p.add_argument("--cells", default="cells")
    p.set_defaults(func=cmd_budgets)

    p = with_packages(sub.add_parser("render", help="draw a cell to a PNG"))
    p.add_argument("cell")
    p.add_argument("--cells", default="cells")
    p.add_argument("--out", default="cell.png")
    p.add_argument("--eye", default="4,4,4", help="camera position, 'x,y,z'")
    p.add_argument("--look-at", default=None, help="target point, 'x,y,z'")
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--backend", default=None,
                   help="force a render backend; omit to use the best "
                        "available (GPU when present, software otherwise)")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("contract", help="print the Scope 13 surface")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("conformance",
                       help="run the accelerator-seam differential suite")
    p.add_argument("--vectors", default=None,
                   help="vector file to check against (default: the committed "
                        "conformance/vectors.json)")
    p.add_argument("--impl", default=None,
                   help="compare this accelerator against the reference "
                        "(e.g. numpy); default checks the reference itself")
    p.add_argument("--emit-vectors", action="store_true",
                   help="print regenerated vectors to stdout (redirect to "
                        "conformance/vectors.json to update them)")
    p.set_defaults(func=cmd_conformance)

    p = sub.add_parser("test", help="run the Scope 14 suite")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_test)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
