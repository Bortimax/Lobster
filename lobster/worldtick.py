"""Unwitnessed outcomes (Scope 6.5) - the structure half.

> **There is no "calculate on approach" path.**
>
> - **Unwitnessed outcomes** resolve once, at event time, via Octopus's world
>   tick: the Event queries the Zone's occupancy index for NPCs, and - closing
>   the v0.2 gap - queries `structures_in_zone` / `structures_in_location` (13)
>   for any structures in range, applying damage to each
>   `StructureState.destroyed_chunks` the same way a player-witnessed hit would.
> - **Cell load applies the pre-computed result** [...] no re-simulation, no
>   stutter.

The queries are "what let the world tick apply mass-casualty structure damage
without Lobster being loaded or involved at all". Almost - a chunk index is
geometry, and Octopus has none, so *something* has to know where a structure
sits and how big its chunks are. That something is the bundle **header**, which
is a few hundred bytes and needs no voxel payload, no mesh and no cell load. See
DECISIONS.md D10.

The result is one MERGE per damaged structure, identical to the operation a
witnessed hit would have written - which is what makes the two paths converge
rather than merely resemble each other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .bundle import BundleError, read_header
from .constants import BUNDLE_SUFFIX, VOXEL_SIZE_M
from .geometry import AABB, Transform, Vec3, sphere_aabb_overlap
from .structure_state import StructureStateWriter, destroy_op


class WorldTickError(Exception):
    pass


@dataclass(frozen=True)
class StructurePlacement:
    """Everything needed to resolve a blast, from the header alone."""

    structure_id: str
    location_id: str
    grid_size: int
    chunk_size: int
    origin: Transform

    @property
    def chunks_per_axis(self) -> int:
        return self.grid_size // self.chunk_size

    @property
    def chunk_count(self) -> int:
        return self.chunks_per_axis ** 3

    def chunk_index(self, cx: int, cy: int, cz: int) -> int:
        c = self.chunks_per_axis
        return cx + cy * c + cz * c * c

    def chunk_aabb(self, index: int, voxel_size: float = VOXEL_SIZE_M) -> AABB:
        c = self.chunks_per_axis
        cx, cy, cz = index % c, (index // c) % c, index // (c * c)
        s = self.chunk_size * voxel_size
        lo = (cx * s, cy * s, cz * s)
        corners = [(x, y, z)
                   for x in (lo[0], lo[0] + s)
                   for y in (lo[1], lo[1] + s)
                   for z in (lo[2], lo[2] + s)]
        return AABB.from_points(self.origin.apply(p) for p in corners)


def placements_for_cell(bundle_dir: str, cell_id: str) -> List[StructurePlacement]:
    """Structure placements read from one bundle header. No payload is touched."""
    path = os.path.join(bundle_dir, cell_id + BUNDLE_SUFFIX)
    if not os.path.exists(path):
        return []
    out: List[StructurePlacement] = []
    for raw in read_header(path).get("structures") or ():
        out.append(StructurePlacement(
            structure_id=raw["structure_id"], location_id=cell_id,
            grid_size=int(raw["grid_size"]),
            chunk_size=int(raw.get("chunk_size", 8)),
            origin=Transform.from_dict(raw.get("origin"))))
    return out


@dataclass(frozen=True)
class BlastResult:
    """What an unwitnessed blast did to the structures in range."""

    center: Vec3
    radius: float
    damaged: Dict[str, Tuple[int, ...]] = dc_field(default_factory=dict)
    ops: Tuple[Dict[str, Any], ...] = ()
    skipped: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"center": list(self.center), "radius": self.radius,
                "damaged": {k: list(v) for k, v in sorted(self.damaged.items())},
                "ops": [dict(o) for o in self.ops],
                "skipped": [dict(s) for s in self.skipped]}


def resolve_blast_in_zone(view: Any, bundle_dir: str, zone_id: str,
                          center: Vec3, radius: float, *,
                          writer: Optional[StructureStateWriter] = None
                          ) -> BlastResult:
    """Damage every structure in a Zone that falls inside a blast radius.

    Uses `structures_in_zone` for *which* structures - Octopus decides zone
    membership - and the bundle headers for *which chunks*. Already-destroyed
    chunks are merged again harmlessly: `UNION_TOMBSTONED` is idempotent, which
    is what lets the witnessed and unwitnessed paths write the same operation.
    """
    records = view.structures_in_zone(zone_id)
    return _resolve(view, bundle_dir, records, center, radius, writer)


def resolve_blast_in_location(view: Any, bundle_dir: str, location_id: str,
                              center: Vec3, radius: float, *,
                              writer: Optional[StructureStateWriter] = None
                              ) -> BlastResult:
    records = view.structures_in_location(location_id)
    return _resolve(view, bundle_dir, records, center, radius, writer)


def _resolve(view: Any, bundle_dir: str, records: Sequence[Dict[str, Any]],
             center: Vec3, radius: float,
             writer: Optional[StructureStateWriter]) -> BlastResult:
    by_location: Dict[str, List[StructurePlacement]] = {}
    damaged: Dict[str, Tuple[int, ...]] = {}
    ops: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for record in records:
        location_id = record.get("location_id")
        if not location_id:
            skipped.append({"record_id": record.get("id"),
                            "reason": "StructureState declares no location_id, "
                                      "so its geometry cannot be located"})
            continue
        if location_id not in by_location:
            try:
                by_location[location_id] = placements_for_cell(bundle_dir,
                                                               location_id)
            except BundleError as e:
                by_location[location_id] = []
                skipped.append({"record_id": record.get("id"),
                                "reason": "cell {0!r} bundle unreadable: "
                                          "{1}".format(location_id, e)})
        placement = next((p for p in by_location[location_id]
                          if p.structure_id == record["id"]), None)
        if placement is None:
            skipped.append({
                "record_id": record.get("id"),
                "reason": "no authored geometry for this StructureState in "
                          "cell {0!r}; a package may declare break-state for a "
                          "structure the bundle does not contain".format(
                              location_id)})
            continue
        hits = tuple(index for index in range(placement.chunk_count)
                     if sphere_aabb_overlap(center, radius,
                                            placement.chunk_aabb(index)))
        if not hits:
            continue
        damaged[placement.structure_id] = hits
        op = destroy_op(placement.structure_id, hits)
        if op is not None:
            ops.append(op)

    # One batch, not a loop of writes. A blast that reaches twenty buildings is
    # the same shape of work as a siege, and it gets the same guarantee: either
    # every structure is written or none is. Half a town flattened because the
    # nineteenth record did not resolve is not an outcome anybody can recover
    # from - see StructureStateWriter.destroy_many.
    if writer is not None and damaged:
        writer.destroy_many(damaged.items())

    return BlastResult(center=tuple(float(c) for c in center),
                       radius=float(radius), damaged=damaged,
                       ops=tuple(ops), skipped=tuple(skipped))


def occupants_in_blast(view: Any, zone_id: str) -> List[str]:
    """Who the world tick should resolve effects for.

    Lobster reports the list and stops there. Scope 7: "resolving 'which of 40
    villagers in a blast radius got hit' is a zone-occupancy query +
    `apply_effect` per occupant" - and `apply_effect` is not on Lobster's
    permitted surface, deliberately. The Event chain applies; Lobster counts.

    **The key is `npc_id`.** An occupant is a `resolve_npc_state` result, and
    that is what the result is keyed by. This read `character_id` or `id` -
    two keys nothing in Octopus produces - so every occupant came back `None`,
    which is precisely the interface drift a thin bridge exists to prevent
    (review L5). A caller targeting Events at that list would have applied
    nothing to everybody.

    An occupant with no `npc_id` raises rather than joining the list as a
    `None`. A blast that reports a casualty it cannot name is worse than one
    that stops: the Event it feeds goes somewhere, and a nameless target is
    not somewhere.
    """
    out: List[str] = []
    for occupant in view.zone_occupants(zone_id):
        npc_id = occupant.get("npc_id")
        if not npc_id:
            raise WorldTickError(
                "zone {0!r}: an occupant has no 'npc_id' ({1!r}). Lobster "
                "reports who is in the blast and applies nothing; an occupant "
                "it cannot name is not reportable.".format(
                    zone_id, sorted(occupant)))
        out.append(npc_id)
    return out
