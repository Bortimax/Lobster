"""Cell loading, residency and transitions (Scope 1, 3, 4, L3, L6).

L3: "A cell is the unit of everything - loading, lighting, navmesh, visibility,
budget."

One cell is one Octopus `Location`. Residency is the Morrowind/Oblivion answer,
which is the whole reason streaming, chunk prioritisation, popping and
seam-hiding are not in this repository:

> **Cell loading, not streaming.** One cell (plus immediate exterior neighbors)
> resident at a time, 1:1 with an Octopus `Location`.

What "immediate exterior neighbors" resolves to is Octopus's `connections`
graph, not a coordinate grid Lobster invents - Octopus has no geometry, and
inventing a second notion of which cells are next to each other would be a
second source of truth for adjacency. Interiors do not preload their
neighbours: a hub with twelve doors would otherwise pull twelve cells into
residency, which is both wrong and the fastest way to blow the transition
budget. See DECISIONS.md D8.

A transition loads before it unloads, so the peak is the union - that is the
budget moment Scope 15.5 warns is "not a loading screen", and `MemoryLedger`
asserts it rather than trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple)

from .budgets import (Budget, BudgetViolation, MemoryLedger, POOL_LIGHTMAP,
                      POOL_METADATA, POOL_NAVMESH, POOL_STRUCTURES,
                      POOL_TERRAIN)
from .bundle import BundleError, CellBundle, read_bundle
from .constants import (BUNDLE_SUFFIX, EXTERIOR_CELL_SIZE_M,
                        RESIDENT_RING, SPATIAL_GRID_CELL_M)
from .events import EventBus
from .geometry import Transform, Vec3
from .navmesh import Navmesh, PatchJob, PatchQueue, recompute_polys
from .octopus_path import ensure_lce_importable
from .skeleton import Skeleton
from .spatial import SNAPSHOT_CELL_ENTRY, SNAPSHOT_CELL_EXIT, SpatialIndex
from .structures import LiveStructure, resolve_break_state
from .tiers import ACTIVE, DORMANT, PROJECTILE

ensure_lce_importable()

import os  # noqa: E402

#: Locations tagged this way are exterior cells and preload their ring.
#: Everything else is treated as an interior, which is the conservative default
#: (fewer cells resident, never more).
EXTERIOR_TAG = "exterior"


class CellError(Exception):
    """A cell that could not be made resident. Always names the cell."""


def is_exterior(view: Any, cell_id: str) -> bool:
    rec = view.record(cell_id) or {}
    return EXTERIOR_TAG in (rec.get("tags") or ())


def exterior_grid(view: Any, cell_id: str) -> Optional[Tuple[int, int]]:
    """A cell's authored [x, z] exterior grid coordinates, if it has any."""
    raw = (view.record(cell_id) or {}).get("exterior_grid")
    if raw is None:
        return None
    if (not isinstance(raw, (list, tuple)) or len(raw) != 2
            or any(isinstance(c, bool) or not isinstance(c, int) for c in raw)):
        raise CellError(
            "cell {0!r}: exterior_grid must be two integers [x, z], got "
            "{1!r}".format(cell_id, raw))
    return (int(raw[0]), int(raw[1]))


def cell_placement(view: Any, cell_id: str, *,
                   cell_size_m: float = EXTERIOR_CELL_SIZE_M) -> Transform:
    """Where this cell's origin sits in the exterior world, if anywhere.

    Exterior cells are laid out on an integer grid and placed at
    `[x * cell_size, 0, z * cell_size]` - the Bethesda scheme, and the reason
    it is the right one is that it is exact: integer coordinates cannot drift,
    adjacency is a comparison rather than a tolerance, and the cell size is
    already a locked constant (Scope 14).

    Interiors return the identity. An interior *is* its own coordinate space -
    it is not anywhere relative to anything, which is precisely what makes the
    Morrowind cell model cheap - so placing one would be inventing a fact.

    Elevation is deliberately not part of this. Height within a cell is the
    terrain heightfield's job, and differences between neighbouring cells are
    baked into their terrain; a per-cell Y offset would be a second way to say
    the same thing.
    """
    grid = exterior_grid(view, cell_id)
    if grid is None:
        return Transform()
    return Transform(position=(grid[0] * cell_size_m, 0.0,
                               grid[1] * cell_size_m))


def residency_ring(view: Any, cell_id: str, *,
                   ring: int = RESIDENT_RING) -> List[str]:
    """Which cells are resident when the player is in `cell_id`.

    The single definition of the residency rule. `CellManager` loads from it and
    the budget checker predicts from it, so a change to what "cell plus its
    ring" means can never be true of one and false of the other.

    An exterior cell brings its exterior neighbours; an interior brings nothing
    (DECISIONS.md D8).
    """
    out = [cell_id]
    if ring <= 0 or not is_exterior(view, cell_id):
        return out
    frontier = [cell_id]
    seen = {cell_id}
    for _ in range(ring):
        nxt: List[str] = []
        for current in frontier:
            for target, _cost in view.connections(current):
                if target in seen or not is_exterior(view, target):
                    continue
                seen.add(target)
                out.append(target)
                nxt.append(target)
        frontier = nxt
    return out


# ---------------------------------------------------------------------------
# Resident cell
# ---------------------------------------------------------------------------

class ResidentCell:
    """One loaded cell: baked geometry + the runtime state derived from records.

    Everything mutable here is derived from `StructureState` and can be thrown
    away and rebuilt by reloading. Nothing in it is saved (L5).
    """

    def __init__(self, bundle: CellBundle, budget: Budget, *,
                 location_record: Optional[Dict[str, Any]] = None) -> None:
        self.bundle = bundle
        self.cell_id = bundle.cell_id
        self.budget = budget
        self.location_record = location_record or {}
        self.terrain = bundle.terrain
        self.navmesh: Optional[Navmesh] = (bundle.navmesh.clone()
                                           if bundle.navmesh else None)
        self.structures: Dict[str, LiveStructure] = {}
        self.skeletons: Dict[str, Skeleton] = {}
        self.quarantined: List[Dict[str, Any]] = []
        grid_m = self.location_record.get("spatial_grid_cell_m") or SPATIAL_GRID_CELL_M
        self.index = SpatialIndex(self.cell_id, cell_size_m=float(grid_m))

    # -- structures ----------------------------------------------------------
    def structure(self, structure_id: str) -> LiveStructure:
        live = self.structures.get(structure_id)
        if live is None:
            raise CellError(
                "cell {0!r}: structure {1!r} is not resident".format(
                    self.cell_id, structure_id))
        return live

    def sound_sources(self) -> List[Dict[str, Any]]:
        """Ambient sound sources, read from the Location record.

        Not from the bundle. Scope 4.5: a mod-supplied sound list "goes through
        the same operation-log/merge/quarantine path as any other package
        content; the build step must not special-case it into a bypass
        channel". Reading the resolved record at load time is what makes that
        true at runtime as well as at build time (DECISIONS.md D7).
        """
        return list(self.location_record.get("sound_sources") or ())

    def spawn_transform(self,
                        from_location_record: Optional[Dict[str, Any]] = None
                        ) -> Transform:
        """Where an arrival into this cell lands (Scope 4).

        > **`connections` -> doors/portals; the connection owns the spawn
        > point.** Matches Oblivion's pattern; lets two doors into the same room
        > arrive at different spots.

        The connection that owns the arrival is the one on the cell being left,
        pointing at this cell - so this takes the *source* Location's record,
        not an id. Anything with no source connection - fast travel, a
        first-time dungeon entry - falls back to `default_spawn_transform`, and
        the build-step lint rejects a Location with neither, so this cannot
        silently drop someone at the origin.
        """
        for conn in (from_location_record or {}).get("connections") or ():
            if not isinstance(conn, dict):
                continue
            if conn.get("target_location_id") != self.cell_id:
                continue
            if conn.get("spawn_transform"):
                return Transform.from_dict(conn["spawn_transform"])
        raw = self.location_record.get("default_spawn_transform")
        if raw:
            return Transform.from_dict(raw)
        raise CellError(
            "cell {0!r}: arrival from {1!r} carries no spawn point and this "
            "Location has no default_spawn_transform. The build-step lint "
            "should have rejected it as unenterable (Scope 4).".format(
                self.cell_id, (from_location_record or {}).get("id")))

    def ambient_at(self, point: Vec3) -> float:
        """Baked light level 0..1 at a position in this cell (Scope 3).

        > Per-cell baked lightmaps; **structures sample ambient at their
        > position**. No per-structure light grid to store or update on damage.

        That second sentence is the whole reason destroying half a keep costs no
        relighting: the surviving chunks sample this, at their position, exactly
        as they did before. Reading the bundle's baked bytes directly keeps the
        runtime independent of `lobster.build` - the baker stays in the build
        step, and this is only the reader.

        A cell with no baked lightmap returns 1.0, so an unbaked or hand-made
        cell renders lit rather than black.
        """
        data = self.bundle.lightmap
        side = self.bundle.lightmap_dims[0] if self.bundle.lightmap_dims else 0
        if not data or side <= 0:
            return 1.0
        voxel = self.bundle.lightmap_voxel_size or 1.0
        ix = int(point[0] // voxel)
        iz = int(point[2] // voxel)
        ix = 0 if ix < 0 else (side - 1 if ix >= side else ix)
        iz = 0 if iz < 0 else (side - 1 if iz >= side else iz)
        index = iz * side + ix
        if index >= len(data):
            return 1.0
        return data[index] / 255.0

    # -- entities ------------------------------------------------------------
    def place(self, entity_id: str, position: Vec3, tier: str, *,
              skeleton: Optional[Skeleton] = None,
              reason: str = SNAPSHOT_CELL_ENTRY) -> None:
        """Put an entity in this cell's spatial index at a known transform.

        Lobster does not decide *who* is in a cell. Scope 4: "What Lobster does
        not invent: occupancy, scheduling, faction state - `queries.zone_occupants`
        already answers that." The caller asks Octopus who is present and tells
        Lobster where to draw them; Lobster owns the where, not the who.
        """
        if tier == ACTIVE:
            already = (entity_id in self.index
                       and self.index.tier_of(entity_id) == ACTIVE)
            self.budget.check("max_active_skeletons",
                              self.index.active_count() + (0 if already else 1),
                              record_id=entity_id)
        self.index.add(entity_id, position, tier, reason=reason)
        if skeleton is not None:
            self.skeletons[entity_id] = skeleton

    def snapshot_entry(self,
                       placements: Iterable[Tuple[str, Vec3, str]]) -> int:
        """Take the cell-entry transform snapshot for every entity at once."""
        placements = list(placements)
        active = sum(1 for _, _, tier in placements if tier == ACTIVE)
        self.budget.check("max_active_skeletons", active,
                          record_id=self.cell_id,
                          detail="ACTIVE-tier entities at cell entry")
        return self.index.snapshot(placements, reason=SNAPSHOT_CELL_ENTRY)

    # -- accounting ----------------------------------------------------------
    def declared_costs(self) -> Dict[str, int]:
        costs = dict(self.bundle.nbytes())
        costs["structures"] += sum(s.nbytes() for s in self.structures.values())
        costs["metadata"] += self.index.nbytes()
        return costs

    def report(self) -> Dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "budget": self.budget.to_dict(),
            "declared_costs": self.declared_costs(),
            "structure_voxels": self.bundle.structure_voxel_count(),
            "micro_chunks": self.bundle.micro_chunk_count(),
            "structures": {sid: live.state_dict()
                           for sid, live in sorted(self.structures.items())},
            "quarantined": list(self.quarantined),
            "navmesh": {
                "polys": len(self.navmesh.polys) if self.navmesh else 0,
                "blocked": sorted(self.navmesh.blocked) if self.navmesh else [],
                "dirty": sorted(self.navmesh.dirty) if self.navmesh else [],
                "version": self.navmesh.version if self.navmesh else 0,
            },
            "spatial": self.index.report(),
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CellManager:
    """Loads, unloads and reports. Decides nothing about the world.

    It does decide *which cells are resident*, which is geometry-layer policy
    about geometry - not gameplay policy - and it does so from the residency
    rule in Scope 1, from Octopus's own connection graph, and from declared
    budgets. There is no heuristic in it and nothing to tune.
    """

    def __init__(self, bundle_dir: str, *, bus: Optional[EventBus] = None,
                 ledger: Optional[MemoryLedger] = None,
                 sink: Any = None, patch_queue: Optional[PatchQueue] = None,
                 ring: int = RESIDENT_RING, session: Any = None) -> None:
        self.bundle_dir = bundle_dir
        #: only used to write item placement (Scope 8). Optional, because every
        #: other thing this manager does is a read.
        self.session = session
        self.bus = bus or EventBus()
        self.ledger = ledger or MemoryLedger()
        self.sink = sink
        self.patches = patch_queue or PatchQueue()
        self.ring = ring
        self.resident: Dict[str, ResidentCell] = {}
        self.player_cell: Optional[str] = None
        self._bundle_cache: Dict[str, CellBundle] = {}

    # -- paths ---------------------------------------------------------------
    def bundle_path(self, cell_id: str) -> str:
        return os.path.join(self.bundle_dir, cell_id + BUNDLE_SUFFIX)

    def _bundle(self, cell_id: str) -> CellBundle:
        """Read a bundle, caching the *immutable* baked data by cell id.

        Caching this is safe in a way caching a query result would not be: a
        bundle is a build artifact with no runtime state in it, and the runtime
        state derived from it (`Navmesh.clone()`, `LiveStructure`) is rebuilt on
        every load.
        """
        cached = self._bundle_cache.get(cell_id)
        if cached is not None:
            return cached
        path = self.bundle_path(cell_id)
        if not os.path.exists(path):
            raise CellError(
                "cell {0!r}: no bundle at {1}. Every Location needs a "
                "`.lobster_cell`; run lobster-build.".format(cell_id, path))
        try:
            bundle = read_bundle(path)
        except BundleError as e:
            raise CellError("cell {0!r}: {1}".format(cell_id, e)) from e
        if bundle.cell_id != cell_id:
            raise CellError(
                "cell {0!r}: bundle at {1} declares cell_id {2!r}".format(
                    cell_id, path, bundle.cell_id))
        self._bundle_cache[cell_id] = bundle
        return bundle

    # -- residency -----------------------------------------------------------
    def is_exterior(self, view: Any, cell_id: str) -> bool:
        return is_exterior(view, cell_id)

    def desired_residency(self, view: Any, cell_id: str) -> List[str]:
        """The cell plus, for an exterior, its immediate exterior neighbours.

        Delegates to the module-level `residency_ring` so the loader and the
        budget checker share one definition of the rule.
        """
        return residency_ring(view, cell_id, ring=self.ring)

    def load(self, view: Any, cell_id: str) -> ResidentCell:
        """Make a cell resident: bundle, break-state, budget, index, Event."""
        existing = self.resident.get(cell_id)
        if existing is not None:
            return existing
        bundle = self._bundle(cell_id)
        location_record = view.record(cell_id)
        budget = Budget.declared(cell_id, location_record)

        # declared content budgets, checked before anything is charged
        budget.check("max_structure_voxels", bundle.structure_voxel_count(),
                     record_id=cell_id,
                     detail="{0} authored structures".format(len(bundle.structures)))
        budget.check("max_micro_chunks", bundle.micro_chunk_count(),
                     record_id=cell_id)

        cell = ResidentCell(bundle, budget, location_record=location_record)

        # break-state: the one save-integrated record (Scope 6)
        for voxel_data in bundle.structures:
            record = view.record(voxel_data.structure_id)
            resolution = resolve_break_state(voxel_data, record)
            if resolution.quarantined:
                cell.quarantined.extend(resolution.quarantined)
            live = LiveStructure(voxel_data, resolution,
                                 location_id=cell_id)
            cell.structures[voxel_data.structure_id] = live

        self._charge(cell)
        self.resident[cell_id] = cell
        self._apply_break_state_to_navmesh(cell)
        self.bus.enter_cell(cell_id)
        return cell

    def _charge(self, cell: ResidentCell) -> None:
        costs = cell.declared_costs()
        bundle = cell.bundle
        # terrain and structures are charged to separate pools, against separate
        # objects - L2's separation is visible in the ledger, not just in the
        # module layout.
        self.ledger.charge(cell.cell_id, POOL_TERRAIN, costs["terrain"],
                           obj=bundle.terrain, budget=cell.budget,
                           record_id=cell.cell_id)
        for live in cell.structures.values():
            self.ledger.charge(cell.cell_id, POOL_STRUCTURES,
                               live.voxel_data.nbytes() + live.nbytes(),
                               obj=live, budget=cell.budget,
                               record_id=live.structure_id)
        self.ledger.charge(cell.cell_id, POOL_NAVMESH, costs["navmesh"],
                           obj=cell.navmesh, budget=cell.budget,
                           record_id=cell.cell_id)
        self.ledger.charge(cell.cell_id, POOL_LIGHTMAP, costs["lightmap"],
                           budget=cell.budget, record_id=cell.cell_id)
        self.ledger.charge(cell.cell_id, POOL_METADATA, costs["metadata"],
                           obj=cell.index, budget=cell.budget,
                           record_id=cell.cell_id)

    def _apply_break_state_to_navmesh(self, cell: ResidentCell) -> None:
        """Break-state loaded from a record is applied to the navmesh
        immediately, not deferred.

        The async allowance in Scope 10 is for destruction that happens *during*
        play, where the alternative is a frame-long stall. At load there is no
        frame to protect and nobody standing on the result yet, so the cell
        becomes resident already consistent - which is what 6.5 means by "cell
        load applies the pre-computed result [...] no re-simulation, no
        stutter".
        """
        if cell.navmesh is None:
            return
        affected: Set[int] = set()
        for structure_id, live in cell.structures.items():
            table = cell.bundle.table_for(structure_id)
            for chunk in live.destroyed:
                affected.update(table.polys_for(chunk))
        if not affected:
            return
        verdicts = recompute_polys(cell.navmesh, affected, terrain=cell.terrain,
                                   structures=list(cell.structures.values()))
        cell.navmesh.apply_patch(verdicts)

    def unload(self, cell_id: str) -> None:
        cell = self.resident.pop(cell_id, None)
        if cell is None:
            return
        # the exit snapshot: a dormant entity's last known-correct transform
        # (Scope 7). Taken before the index goes away, not after.
        cell.index.snapshot(
            [(e.entity_id, e.position, e.tier) for e in cell.index.entries()],
            reason=SNAPSHOT_CELL_EXIT)
        self.ledger.release(cell_id)
        self.bus.exit_cell(cell_id)

    def is_continuous_move(self, view: Any, cell_id: str, *,
                           from_location_id: Optional[str] = None) -> bool:
        """Can the player get to `cell_id` from where they are without a jump?

        True when the destination is already resident (they can see it) or an
        authored connection leads there from the cell they are leaving (a door,
        a path). False for fast travel, first-time dungeon entry, a scripted
        relocation - anything Scope describes as *"an entry that did not come
        through an authored connection"*.

        Public because the answer is a fact about geometry residency that a
        caller may want before it commits: it is exactly the question "does
        this need a loading screen". Lobster reports it and decides nothing
        about it (L4).
        """
        if cell_id in self.resident:
            return True
        origin = from_location_id or self.player_cell
        if origin is None:
            return False
        return any(target == cell_id for target, _cost in view.connections(origin))

    def set_player_cell(self, view: Any, cell_id: str, *,
                        from_location_id: Optional[str] = None) -> ResidentCell:
        """The player moved to `cell_id`. Load first, then unload - **if they
        walked.** If they jumped, release first (Shrimp finding #6, D32).

        **A walk holds the union.** Adjacent cells are in each other's rings, so
        for the length of this call both residency sets are charged and the
        ledger asserts the union fits. That is the whole reason a transition has
        a peak worth budgeting, and unloading first would hide the cost and drop
        the floor out from under anything still standing in the old cell.

        **A jump has no continuity to preserve**, and holding it is what broke.
        Two exterior rings that share nothing sum to `2 x ring`, which for the
        4-connected case is `2 x 5 = 10` against a ceiling of 9. That is
        structural arithmetic, not a content mistake, and fast travel is a
        documented first-class entry path (`default_spawn_transform` names it),
        so the first player to use it hit a `BudgetViolation` naming a third
        cell with nothing to do with either endpoint.

        **The test for "jump" is that the destination is neither resident nor
        connected to where the player is standing.** Residency alone is not
        enough, and the first draft of this fix got it wrong: an interior is
        never in an exterior's ring (D8), so "not resident" would classify
        every walk through a keep door as a teleport and quietly delete the
        transition peak that `MAX_TRANSITION_PEAK_BYTES` exists to bound. The
        connection check restores it - a door is an authored edge, and walking
        through one is continuous even though the room behind it was not
        preloaded.

        That leaves exactly the case Scope names: an entry that did not come
        through an authored connection. Nothing is gained by holding the old
        set across one - the player is not in it, and no geometry there needs
        to survive the frame.

        Cells in both sets are untouched either way: `load` is idempotent and
        `unload` only takes cells outside `desired`, so a jump whose rings
        happen to overlap keeps the overlap rather than churning it.

        One observable consequence, since §13 fixes the Events but not their
        order: across a jump `on_exit_cell` now precedes `on_enter_cell`. That
        is the truer sequence for a teleport anyway - you leave, then you
        arrive - and a walk is unchanged.
        """
        desired = self.desired_residency(view, cell_id)
        walked = self.is_continuous_move(view, cell_id,
                                         from_location_id=from_location_id)
        doomed = [c for c in self.resident if c not in desired]

        if not walked:
            for cid in doomed:
                self.unload(cid)
        for cid in desired:
            self.load(view, cid)
        if walked:
            for cid in doomed:
                self.unload(cid)
        self.player_cell = cell_id
        cell = self.resident[cell_id]
        if self.sink is not None:
            self.sink.enter_scene(cell_id)
        return cell

    def placements(self, view: Any) -> Dict[str, Transform]:
        """Where each resident cell sits, for anything drawing more than one.

        `lobster.visibility` refuses to invent this (D21), so the manager - the
        one object that knows which cells are resident - is where it comes
        from. Interiors resolve to the identity, so a draw of an interior plus
        its (empty) ring behaves exactly as it did before placements existed.
        """
        return {cell_id: cell_placement(view, cell_id)
                for cell_id in sorted(self.resident)}

    def spawn_transform(self, view: Any, cell_id: str,
                        from_location_id: Optional[str] = None) -> Transform:
        """Arrival transform for entering `cell_id` from `from_location_id`."""
        cell = self.resident.get(cell_id)
        if cell is None:
            raise CellError(
                "cell {0!r} is not resident; load it before asking where an "
                "arrival lands".format(cell_id))
        source = view.record(from_location_id) if from_location_id else None
        return cell.spawn_transform(source)

    # -- items (Scope 8) -----------------------------------------------------
    def items_in(self, view: Any, cell_id: str) -> List[Any]:
        """Every item physically in a resident cell, read fresh.

        Read rather than cached, for the reason §13 gives about every live
        query: an item somebody else moved this frame has moved. The read costs
        one permitted query (D34) over an index that is O(items in the cell).
        """
        from .items import placed_items
        if cell_id not in self.resident:
            raise CellError(
                "cell {0!r} is not resident; load it before asking what is "
                "lying about in it".format(cell_id))
        return placed_items(view, cell_id)

    def place_item(self, view: Any, item_id: str, cell_id: str,
                   transform: Transform, *, placer: Any = None) -> Any:
        """Put an item's representation in the world (Scope 8).

        Refuses a cell that is not resident. That is not fussiness: the
        transform is in that cell's coordinates, and a caller placing into a
        cell Lobster cannot see is describing a position it cannot check
        against terrain, structures or the cell's own extent. Loud beats a
        sword resolving inside a hill.

        `placer` is injectable so a caller may batch writes or supply its own
        session; by default one is built from this manager's own.
        """
        from .items import ItemPlacer
        if cell_id not in self.resident:
            raise CellError(
                "cannot place {0!r} into {1!r}: that cell is not resident, so "
                "its coordinates mean nothing here".format(item_id, cell_id))
        writer = placer if placer is not None else ItemPlacer(
            self._session_for_writes(view), self.bus)
        return writer.place(item_id, cell_id, transform)

    def remove_item(self, view: Any, item_id: str, *,
                    placer: Any = None) -> Optional[Any]:
        """Take an item's representation back out of the world.

        Returns what was removed, or None if it was not in the world to begin
        with - which is not an error: two systems racing to pick up the same
        sword is ordinary, and the loser should get a null rather than an
        exception.
        """
        from .items import ItemPlacer, PlacedItem
        from .octopus_bridge import resolve_item_location
        record = view.record(item_id)
        if not record or not record.get("world_transform")                 or not resolve_item_location(record):
            return None
        placed = PlacedItem.from_record(record)
        writer = placer if placer is not None else ItemPlacer(
            self._session_for_writes(view), self.bus)
        writer.remove(placed.item_id, placed.cell_id, placed.transform)
        return placed

    def _session_for_writes(self, view: Any) -> Any:
        session = self.session if self.session is not None else getattr(
            view, "session", None)
        if session is None:
            raise CellError(
                "this CellManager has no session, so it cannot write item "
                "placement; construct it with session=, or pass placer=")
        return session

    # -- damage --------------------------------------------------------------
    def damage_structure(self, view: Any, cell_id: str, structure_id: str,
                         chunk_indices: Iterable[int], *,
                         writer: Any = None) -> Dict[str, Any]:
        """Destroy chunks: collider now, navmesh later (Scope 10, v0.4).

        Order is load-bearing, not stylistic:

        1. `LiveStructure.destroy_chunks` updates the voxel state, and with it
           the collider and the visual mesh, **on this frame**. `geometry_version`
           moves; a player standing in the rubble the instant it forms is
           standing on the rubble.
        2. The `StructureState` write and `on_structure_damaged` follow.
        3. Only if a destroyed chunk was inferred load-bearing does a navmesh
           patch get *queued*. The affected polys go dirty immediately, so
           pathing routes around them or holds, and the recompute lands whenever
           `pump_navmesh` gets to it.

        Nothing here waits on step 3, and step 3 cannot reach back into step 1.
        """
        cell = self.resident.get(cell_id)
        if cell is None:
            raise CellError(
                "cell {0!r} is not resident; damage to {1!r} in an unloaded "
                "cell goes through the world-tick path (Scope 6.5), not "
                "here.".format(cell_id, structure_id))
        live = cell.structure(structure_id)

        newly = live.destroy_chunks(chunk_indices)      # (1) synchronous
        if not newly:
            return {"structure_id": structure_id, "destroyed": [],
                    "navmesh_job": None, "geometry_version": live.geometry_version}

        if writer is not None:                           # (2) remembered
            writer.destroy(structure_id, newly)
        else:
            self.bus.structure_damaged(structure_id, newly)

        job = None                                       # (3) deferred
        table = cell.bundle.table_for(structure_id)
        polys: Set[int] = set()
        for chunk in newly:
            polys.update(table.polys_for(chunk))
        if polys and cell.navmesh is not None:
            job = self.patches.submit(
                PatchJob(cell_id=cell_id, poly_ids=tuple(sorted(polys)),
                         cause_structure_id=structure_id,
                         cause_chunks=tuple(newly),
                         adjacent_cell_ids=tuple(self._affected_neighbours(
                             view, cell_id))),
                cell.navmesh)
        return {"structure_id": structure_id, "destroyed": newly,
                "navmesh_job": job.to_dict() if job else None,
                "geometry_version": live.geometry_version}

    def _affected_neighbours(self, view: Any, cell_id: str) -> List[str]:
        """Adjacent cells whose connections into this one may be affected.

        Scope 10.3 extends the recompute to "any adjacent cell whose connections
        into this cell are affected" - bounded to the same ring residency
        already uses, so the worst case stays "cell + neighbours" and never
        becomes a world-wide sweep.
        """
        return sorted({target for target, _ in view.connections(cell_id)})

    def pump_navmesh(self, *, max_jobs: int = 1) -> List[Dict[str, Any]]:
        """Run pending navmesh patches. Called by the game loop, never by the
        damage path."""
        def resolve(job: PatchJob) -> Dict[int, bool]:
            cell = self.resident.get(job.cell_id)
            if cell is None or cell.navmesh is None:
                return {}
            verdicts = recompute_polys(
                cell.navmesh, job.poly_ids, terrain=cell.terrain,
                structures=list(cell.structures.values()))
            cell.navmesh.apply_patch(verdicts)
            return verdicts
        return self.patches.pump(resolve, max_jobs=max_jobs)

    # -- reporting -----------------------------------------------------------
    def report(self) -> Dict[str, Any]:
        return {
            "player_cell": self.player_cell,
            "resident": sorted(self.resident),
            "ledger": self.ledger.report(),
            "pending_navmesh_patches": len(self.patches),
            "cells": {cid: cell.report()
                      for cid, cell in sorted(self.resident.items())},
        }

    def quarantined(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for cell in self.resident.values():
            out.extend(cell.quarantined)
        return out
