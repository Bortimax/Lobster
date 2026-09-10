"""GPU buffer lifetime, driven by the residency Events (RENDER_SCOPE §4).

Static geometry should reach the GPU **once per residency**, not once per
frame. That is the entire reason `upload_cell` and `release_cell` are on a
GPU-shaped interface — and until now nothing called them.

## Why the events and not a call from `CellManager`

The obvious wiring is `CellManager.load` calling `backend.upload_cell`. It is
also wrong: `cell.py` is renderer-free today, residency is geometry, and making
the geometry layer reach into presentation is Lobster's own L8 mistake at a
different boundary.

`on_enter_cell` and `on_exit_cell` already exist, are already fired by
`load`/`unload`, and CONTRACT §1 already says what they mean:

> loading a neighbouring cell for residency is not the player entering a scene

That is buffer lifetime, exactly. So the backend becomes another subscriber to
a bus that is already there, the manager learns nothing, and no module gains an
import.

## The ordering this relies on, which is not accidental

* `load` inserts into `resident` **before** firing `on_enter_cell`, so the cell
  can be looked up when the handler runs.
* `load` returns early when the cell is already resident, so the Event does not
  fire twice and a cell cannot be uploaded twice.
* `unload` pops from `resident` **before** firing `on_exit_cell`. The cell is
  gone by then — which is fine, because releasing needs only the id, and it is
  why this class keeps its own record of what it uploaded rather than asking
  the manager.

That last point matters for fast travel (D32), where the old ring is released
*before* the new one loads. The release handler cannot consult `manager.resident`
because the answer would be about the wrong moment.

## Shared models are reference-counted, and cells are not

A cell's buffers belong to that cell: one uploader, one releaser, a set. A
*model* is shared - fifty barrels in a town are fifty placements of one buffer
(ASSET_SCOPE §2) - so the question "may this be released" has as many answers as
there are resident cells, and the answer is a count.

A refcount is the exact class of bug D43 recorded: **every answer stays correct
while the memory grows.** The leaked reference in the C kernel passed 2,400
differential cases, because those compare outputs, and a model that is never
released draws perfectly. So the counts are not trusted to be right by
construction - `drift()` extends to them and asserts the thing that actually
matters: *the set of live models equals the set of models the resident cells
reference*, after any sequence of loads and unloads whatsoever.

## Break-state is the one dynamic case

Terrain and structure meshes are static per residency, except that destroying a
micro-chunk changes structure geometry *at runtime* — `geometry_version` moves
on the frame the wall comes down. That is a re-upload of the touched structure,
driven by `on_structure_damaged`, and it is the third consumer of a contract
Event.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Set, Tuple


class ResidencyError(Exception):
    pass


@dataclass
class ResidencyStats:
    """What the binding did. The input to every assertion about it."""

    uploads: int = 0
    releases: int = 0
    reuploads: int = 0
    #: uploads skipped because the cell was already on the GPU
    duplicate_uploads_suppressed: int = 0
    #: releases for cells that were never uploaded
    unknown_releases: int = 0
    #: models uploaded, which is once each however many cells reference them
    model_uploads: int = 0
    model_releases: int = 0
    #: retains that found the model already live - the saving, counted
    model_uploads_shared: int = 0
    #: a model release for something never uploaded. Counted, not obeyed - the
    #: same shape as `unknown_releases`.
    unknown_model_releases: int = 0
    #: references to models the library does not hold. Named, not swallowed:
    #: the symptom is otherwise a thing that does not appear.
    missing_models: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {"uploads": self.uploads, "releases": self.releases,
                "reuploads": self.reuploads,
                "duplicate_uploads_suppressed":
                    self.duplicate_uploads_suppressed,
                "unknown_releases": self.unknown_releases,
                "model_uploads": self.model_uploads,
                "model_releases": self.model_releases,
                "model_uploads_shared": self.model_uploads_shared,
                "unknown_model_releases": self.unknown_model_releases,
                "missing_models": self.missing_models}


class GpuResidency:
    """Keeps a backend's buffers in step with what is resident.

    Holds its own set of uploaded cell ids rather than trusting the manager,
    because the release Event arrives *after* the cell has left `resident` and
    because that set is the thing a leak test needs to compare against.
    """

    def __init__(self, backend: Any, manager: Any) -> None:
        self.backend = backend
        self.manager = manager
        self.uploaded: Set[str] = set()
        #: model_ref -> how many resident cells reference it. A key is present
        #: exactly while the model is live on the backend.
        self.model_counts: Dict[str, int] = {}
        #: cell_id -> what that cell retained. Remembered for the same reason
        #: `uploaded` is: the release Event arrives after the cell has left
        #: `resident`, so the answer cannot be recomputed at that moment.
        self.retained_by: Dict[str, Tuple[str, ...]] = {}
        #: model refs nothing in the library could satisfy, and the cells that
        #: asked. Not a count alone - a name, so the failure is attributable.
        self.unresolved: Dict[str, Set[str]] = {}
        self.stats = ResidencyStats()

    # -- wiring --------------------------------------------------------------
    def attach(self, bus: Any) -> "GpuResidency":
        """Subscribe to the residency Events. Returns self, so a caller can
        write `GpuResidency(backend, manager).attach(bus)`."""
        from ..events import (ON_ENTER_CELL, ON_EXIT_CELL,
                              ON_STRUCTURE_DAMAGED)
        bus.subscribe(ON_ENTER_CELL, self.on_enter_cell)
        bus.subscribe(ON_EXIT_CELL, self.on_exit_cell)
        bus.subscribe(ON_STRUCTURE_DAMAGED, self.on_structure_damaged)
        return self

    # -- handlers ------------------------------------------------------------
    def on_enter_cell(self, event: Any) -> None:
        cell_id = event.location_id
        if cell_id in self.uploaded:
            # `load` is idempotent and does not re-fire, so reaching here means
            # something else fired the Event. Counted rather than raised: the
            # bus is public and a caller replaying an Event is not a crime.
            self.stats.duplicate_uploads_suppressed += 1
            return
        cell = self.manager.resident.get(cell_id)
        if cell is None:
            raise ResidencyError(
                "on_enter_cell({0!r}) arrived with the cell not resident. "
                "`load` inserts before it fires, so this means the Event was "
                "raised by something that is not residency - and uploading "
                "geometry for a cell nobody can see would leak it".format(
                    cell_id))
        self.backend.upload_cell(cell)
        self.uploaded.add(cell_id)
        self.stats.uploads += 1
        self._retain_models(cell)

    def on_exit_cell(self, event: Any) -> None:
        cell_id = event.location_id
        if cell_id not in self.uploaded:
            self.stats.unknown_releases += 1
            return
        self.backend.release_cell(cell_id)
        self.uploaded.discard(cell_id)
        self.stats.releases += 1
        self._release_models(cell_id)

    def on_structure_damaged(self, event: Any) -> None:
        """A wall came down, so that structure's geometry moved this frame.

        The Event carries no cell id, so the owning cell is found by asking the
        resident set - at most `MAX_RESIDENT_CELLS` lookups, and only on the
        frame something was destroyed.
        """
        cell_id = self._cell_of_structure(event.structure_id)
        if cell_id is None or cell_id not in self.uploaded:
            return
        cell = self.manager.resident.get(cell_id)
        if cell is None:                                 # pragma: no cover
            return
        self.backend.upload_structure(cell, event.structure_id,
                                      tuple(event.chunk_indices))
        self.stats.reuploads += 1

    # -- shared models -------------------------------------------------------
    def _retain_models(self, cell: Any) -> None:
        """Count one reference per *distinct* model this cell places.

        Distinct is the point: a cell with fifty barrels holds one reference, so
        unloading it releases the model once. Counting per placement would also
        balance arithmetically and would make the count mean something else -
        and the invariant `drift()` asserts is about the set, not the total.
        """
        refs = tuple(cell.model_refs())
        self.retained_by[cell.cell_id] = refs
        library = self._library()
        for model_ref in refs:
            count = self.model_counts.get(model_ref, 0)
            if count:
                self.model_counts[model_ref] = count + 1
                self.stats.model_uploads_shared += 1
                continue
            mesh = library.models.get(model_ref) if library else None
            if mesh is None:
                # Never counted, so nothing tries to release it later. The cell
                # that asked is remembered because "a model is missing" names
                # nobody.
                self.unresolved.setdefault(model_ref, set()).add(cell.cell_id)
                self.stats.missing_models += 1
                continue
            self.backend.upload_model(mesh)
            self.model_counts[model_ref] = 1
            self.stats.model_uploads += 1

    def _release_models(self, cell_id: str) -> None:
        for model_ref in self.retained_by.pop(cell_id, ()):
            count = self.model_counts.get(model_ref, 0)
            if count <= 0:
                # Retained by this cell but no longer counted. Obeying it would
                # take a live model away from a cell still showing one, so it is
                # counted instead - `unknown_releases` with a different subject.
                self.stats.unknown_model_releases += 1
                continue
            if count > 1:
                self.model_counts[model_ref] = count - 1
                continue
            del self.model_counts[model_ref]
            self.backend.release_model(model_ref)
            self.stats.model_releases += 1
        for cells in self.unresolved.values():
            cells.discard(cell_id)
        self.unresolved = {ref: cells for ref, cells in self.unresolved.items()
                           if cells}

    def _library(self) -> Any:
        return getattr(self.manager, "library", None)

    def live_models(self) -> List[str]:
        """Which models are on the backend right now."""
        return sorted(self.model_counts)

    def unresolved_models(self) -> Dict[str, List[str]]:
        """model_ref -> the resident cells that asked for it and did not get
        it. Empty in a world whose build passed lint."""
        return {ref: sorted(cells)
                for ref, cells in sorted(self.unresolved.items())}

    def _cell_of_structure(self, structure_id: str) -> Optional[str]:
        for cell_id in sorted(self.uploaded):
            cell = self.manager.resident.get(cell_id)
            if cell is not None and structure_id in getattr(cell, "structures",
                                                            {}):
                return cell_id
        return None

    # -- the leak check ------------------------------------------------------
    def drift(self) -> Dict[str, List[str]]:
        """Where the GPU and the resident set disagree.

        `{"leaked": [...], "missing": [...]}` — uploaded but no longer
        resident, and resident but never uploaded. Both should always be empty;
        this exists so a test can say so in one line, and because D43 is a
        fresh reminder that resource bugs pass every test that only checks
        answers.
        """
        resident = set(self.manager.resident)
        library = self._library()
        held = set(library.models) if library else set()
        # What the resident cells *actually* reference, recomputed rather than
        # accumulated: a count compared against its own history would agree with
        # itself while both were wrong. Refs the library cannot satisfy are
        # excluded and reported by `unresolved_models` instead - they can never
        # be live, so counting them here would make a content typo look like a
        # permanent leak.
        needed = {ref for cell_id in resident
                  for ref in self.manager.resident[cell_id].model_refs()
                  if ref in held}
        live = set(self.model_counts)
        return {"leaked": sorted(self.uploaded - resident),
                "missing": sorted(resident - self.uploaded),
                "models_leaked": sorted(live - needed),
                "models_missing": sorted(needed - live)}

    def in_step(self) -> bool:
        return not any(self.drift().values())
