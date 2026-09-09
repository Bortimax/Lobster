"""Build-step inference of `navmesh_load_bearing` (Scope 10, v0.4).

> `lobster-build` bakes the cell's navmesh as usual, then, for every
> micro-chunk in every structure in that cell, tests whether the chunk's
> bounding volume intersects any navmesh polygon. If it does, the chunk is
> automatically flagged `navmesh_load_bearing: true`; if not, `false`. No
> authoring step required for the common case.

This is deliberately in the *build* package and not in the runtime. Scope 15.7:

> If `navmesh_load_bearing` ever goes back to being an author-set flag instead
> of a build-step inference, that's a regression - it reopens the exact
> silent-failure mode (a forgotten flag on a gatehouse, NPCs walking through
> rubble with no error) that v0.4 exists to close.

Two properties worth stating plainly:

**It fails safe, not silent.** Over-flagging a decorative arch costs one
navmesh recompute that turns out to have changed nothing. Under-flagging costs
NPCs walking through rubble, with no error anywhere. Every approximation here -
the AABB re-bound of a rotated chunk, the vertical margin on a polygon - leans
towards over-flagging on purpose.

**Overrides are visible.** An author may opt a chunk out, and the override is
carried alongside the inference rather than replacing it, so a build report can
show both. An inference that fails safe is only useful if a writer can see what
it decided.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..constants import VOXEL_SIZE_M
from ..navmesh import LoadBearingTable, Navmesh
from ..structures import StructureVoxelData


def infer_load_bearing(navmesh: Optional[Navmesh],
                       structure: StructureVoxelData, *,
                       overrides: Optional[Mapping[int, bool]] = None,
                       voxel_size: float = VOXEL_SIZE_M,
                       skip_empty_chunks: bool = True) -> LoadBearingTable:
    """Which micro-chunks of this structure touch the navmesh, and which polys.

    Empty chunks are skipped by default: a chunk with no solid voxels cannot
    support or block anything, so flagging it would queue recomputes for
    destroying nothing. That is a cost decision, not a correctness one - an
    empty chunk being destroyed changes no geometry at all.
    """
    inferred: Dict[int, Tuple[int, ...]] = {}
    if navmesh is not None:
        for index in structure.chunk_indices():
            if skip_empty_chunks and structure.chunk_is_empty(index):
                continue
            box = structure.chunk_aabb_world(index, voxel_size)
            polys = navmesh.polys_intersecting(box)
            if polys:
                inferred[index] = tuple(polys)
    return LoadBearingTable(structure_id=structure.structure_id,
                            inferred=inferred,
                            overrides=dict(overrides or {}))


def inference_report(table: LoadBearingTable,
                     structure: StructureVoxelData) -> Dict[str, Any]:
    """What the build step tells the author.

    An inference nobody can read is an inference nobody can correct, so this
    names the counts and every override, with the value it overrode.
    """
    overridden = []
    for chunk, value in sorted(table.overrides.items()):
        overridden.append({
            "chunk_index": chunk,
            "inferred": bool(table.inferred.get(chunk)),
            "override": bool(value),
            "effect": ("suppresses an inferred load-bearing chunk"
                       if table.inferred.get(chunk) and not value
                       else "forces a chunk the inference did not flag"
                       if value and not table.inferred.get(chunk)
                       else "agrees with the inference (no effect)"),
        })
    return {
        "structure_id": structure.structure_id,
        "total_chunks": structure.chunk_count,
        "inferred_load_bearing": len(table.inferred),
        "load_bearing_after_overrides": sum(
            1 for i in structure.chunk_indices() if table.is_load_bearing(i)),
        "overrides": overridden,
    }


def validate_overrides(table: LoadBearingTable,
                       structure: StructureVoxelData) -> List[Dict[str, Any]]:
    """Build-step findings for overrides that point at nothing.

    An override on a chunk index that does not exist is dead authoring - most
    often left behind when a structure was resized - and silently ignoring it
    is how an author comes to believe a gate is opted out when it is not.
    """
    findings: List[Dict[str, Any]] = []
    for chunk in sorted(table.overrides):
        if chunk < 0 or chunk >= structure.chunk_count:
            findings.append({
                "code": "override_out_of_range",
                "record_id": structure.structure_id,
                "detail": "navmesh_load_bearing override targets chunk {0}, "
                          "but this structure has {1} chunks".format(
                              chunk, structure.chunk_count)})
        elif table.overrides[chunk] and not table.inferred.get(chunk):
            findings.append({
                "code": "override_forces_unflagged_chunk",
                "record_id": structure.structure_id,
                "detail": "chunk {0} is forced load-bearing but does not "
                          "intersect any navmesh polygon; it will queue "
                          "recomputes that can never change anything".format(
                              chunk)})
    return findings
