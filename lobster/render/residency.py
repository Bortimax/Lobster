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

## Break-state is the one dynamic case

Terrain and structure meshes are static per residency, except that destroying a
micro-chunk changes structure geometry *at runtime* — `geometry_version` moves
on the frame the wall comes down. That is a re-upload of the touched structure,
driven by `on_structure_damaged`, and it is the third consumer of a contract
Event.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Set


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

    def to_dict(self) -> Dict[str, int]:
        return {"uploads": self.uploads, "releases": self.releases,
                "reuploads": self.reuploads,
                "duplicate_uploads_suppressed":
                    self.duplicate_uploads_suppressed,
                "unknown_releases": self.unknown_releases}


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

    def on_exit_cell(self, event: Any) -> None:
        cell_id = event.location_id
        if cell_id not in self.uploaded:
            self.stats.unknown_releases += 1
            return
        self.backend.release_cell(cell_id)
        self.uploaded.discard(cell_id)
        self.stats.releases += 1

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
        return {"leaked": sorted(self.uploaded - resident),
                "missing": sorted(resident - self.uploaded)}

    def in_step(self) -> bool:
        return not any(self.drift().values())
