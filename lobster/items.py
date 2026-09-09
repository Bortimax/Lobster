"""Placing and removing an item's representation in the world (Scope 8).

> | **Lobster** | Object selection/raycasting, world-space labels, physically
> placing/removing an item's representation in the world. |
> | **Octopus** | Inventory data - `Item` records, `resolve_equipment_slots`. |

So this module moves *where a thing is*, and knows nothing about what it is.
There is no pickup, no inventory, no weight, no stack count, no "can the player
reach it" - all of that is Shrimp's and Octopus's (L4, L8). What Lobster owns is
the fact that a sword is lying at a particular spot in a particular cell, and
the two Events that say so.

**Where an item is comes from Octopus; whether it is *manifested* comes from
here.** Placement is two fields - content's `default_location_ref` and the
save's `current_location_ref`, which wins when set (Octopus D52) - and
`world_transform` says where in that cell the representation sits. Placement
writes the transform and then the save-layer location, so no reader sees a
half-placed item as placed. Removal writes only the transform, because the
location is where the item *went* and that is not Lobster's to invent.

**Writes go down the ordinary Octopus path.** `world_transform` is a normal
record field on a normal record, written with a normal `PATCH`, resolved by
normal layering. Nothing here special-cases it, and a mod, a console command or
a save-editor changing the same field behaves identically to Lobster changing
it. That is the same discipline `StructureStateWriter` follows for
`destroyed_chunks`, and it is why break-state survived a mod being uninstalled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import SPATIAL_GRID_CELL_M
from .events import EventBus
from .geometry import Transform, Vec3
from .spatial import bucket_of, dilated_segment_walk, span_for

ITEM_TYPE = "Item"
WORLD_TRANSFORM = "world_transform"
LOCATION_REF = "current_location_ref"


class ItemError(Exception):
    """A placement that cannot be represented. Says which item and why."""


@dataclass(frozen=True)
class PlacedItem:
    """An item's representation in a resident cell. Geometry, not inventory.

    Derived from the `Item` record every time a cell loads; never persisted,
    never a source of truth. Throw it away and reload and it is identical.
    """

    item_id: str
    cell_id: str
    transform: Transform
    #: `Item.model_ref`, passed through untouched. Lobster does not resolve it:
    #: there is no asset pipeline, and inventing one under the heading "place
    #: an item's representation" would be exactly the scope creep L8 forbids.
    model_ref: str = ""

    @property
    def position(self):
        return self.transform.position

    def to_dict(self) -> Dict[str, Any]:
        return {"item_id": self.item_id, "cell_id": self.cell_id,
                "transform": self.transform.to_dict(),
                "model_ref": self.model_ref}

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "PlacedItem":
        """Build one from an `Item` that `items_in_location` returned.

        Where it is comes from `resolve_item_location`, not from a raw field
        read: placement is `current_location_ref` falling back to
        `default_location_ref` (Octopus D52), and re-deriving that here is the
        mistake D52 named in advance. The first version of this method did read
        the raw field, and a mod-placed sword raised instead of appearing.

        `items_in_location` already filters out unmanifested items, so this
        asserts the invariant rather than tolerating a breach: reaching here
        with a missing half means something bypassed both the lint and the
        index.
        """
        from .octopus_bridge import resolve_item_location
        transform = record.get(WORLD_TRANSFORM)
        cell_id = resolve_item_location(record)
        if not transform or not cell_id:
            raise ItemError(
                "{0!r} is not placed: world_transform={1!r}, resolved "
                "location={2!r}. An item in the world needs both (CONTRACT "
                "section 2)".format(record.get("id"), transform, cell_id))
        return cls(item_id=record["id"], cell_id=cell_id,
                   transform=Transform.from_dict(transform),
                   model_ref=record.get("model_ref") or "")


def placed_items(view: Any, cell_id: str) -> List[PlacedItem]:
    """Every item physically in this cell, nearest thing to a constructor.

    One permitted query (`items_in_location`, D34) and no interpretation.
    """
    return [PlacedItem.from_record(rec) for rec in view.items_in_location(cell_id)]


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------

def place_ops(item_id: str, cell_id: str,
              transform: Transform) -> List[Dict[str, Any]]:
    """The operations that put an item in the world. **Order is load-bearing.**

    Octopus has no multi-field write - `PATCH` takes one `field` and one
    `value` (`lce/ops.py`) - so the co-null invariant cannot be established in
    a single op, and claiming otherwise would be a lie about durability. What
    *can* be guaranteed is that **every intermediate state is "not in the
    world"**, so no reader ever sees a half-placed item as placed:

    1. `world_transform` first. The item now has a position and no cell, so
       `build_item_index` does not bucket it and nothing draws, picks or
       labels it. It is exactly as absent as it was a moment ago.
    2. `current_location_ref` second. **This is the commit point** - the index
       keys on it, so the item arrives on this write and not before.

    Removal needs no such ordering: it is a single write (see `remove_ops`).

    A crash between the two leaves a half-placed record, which is precisely
    what `item_transform_without_location` exists to catch on the next build,
    and which the index skips at runtime meanwhile. Loud on inspection, inert
    in play - the failure mode to prefer.
    """
    return [{"op": "PATCH", "id": item_id, "field": WORLD_TRANSFORM,
             "value": transform.to_dict()},
            {"op": "PATCH", "id": item_id, "field": LOCATION_REF,
             "value": cell_id}]


def remove_ops(item_id: str) -> List[Dict[str, Any]]:
    """Take the representation out of the world. **One field, deliberately.**

    An earlier version cleared the location too, and that was wrong twice over
    once Octopus's placement model was read properly (D52, D35):

    1. **It cannot work.** `current_location_ref` falls back to content's
       `default_location_ref` when unset, so nulling it does not remove a
       mod-placed sword from its table - it restores it there.
    2. **It is not Lobster's to say.** Octopus has no inventory record type:
       *"carried by X is just an item located at X"*. So the location field is
       where the item **went**, and Lobster does not know whether that is a
       backpack, a chest or nowhere. Writing it would be guessing, which is
       policy (L4).

    Clearing `world_transform` alone is sufficient and honest: the index keys
    presence on it, so the representation leaves the world on this write
    whatever any location field says. The caller records where it went, if it
    went anywhere.
    """
    return [{"op": "PATCH", "id": item_id, "field": WORLD_TRANSFORM,
             "value": None}]


class ItemPlacer:
    """Writes item placement, then reports it. In that order.

    The same two responsibilities `StructureStateWriter` has, for the same
    reason: write it so it is remembered, fire the Event so somebody else can
    decide what it means. It never interprets - nothing here knows whether the
    player dropped the sword, an NPC did, or a chest was smashed open.
    """

    def __init__(self, session: Any, bus: Optional[EventBus] = None) -> None:
        self.session = session
        self.bus = bus
        self.written: List[Dict[str, Any]] = []

    def _write(self, op: Dict[str, Any]) -> Dict[str, Any]:
        engine = getattr(self.session, "engine", None)
        if engine is not None and hasattr(engine, "write"):
            tagged = engine.write(op)
        else:
            tagged = self.session.save.append(op)
        self.written.append(tagged)
        return tagged

    def place(self, item_id: str, cell_id: str,
              transform: Transform) -> PlacedItem:
        """Put an item in the world and say so."""
        if not isinstance(transform, Transform):
            raise ItemError(
                "{0!r}: place needs a Transform, not {1}".format(
                    item_id, type(transform).__name__))
        for op in place_ops(item_id, cell_id, transform):
            self._write(op)
        if self.bus is not None:
            self.bus.item_placed(item_id, cell_id, transform)
        return PlacedItem(item_id=item_id, cell_id=cell_id, transform=transform)

    def remove(self, item_id: str, cell_id: str,
               transform: Transform) -> None:
        """Take the representation out of the world, reporting where it was.

        `on_item_removed` carries the cell and transform it *had*, not where it
        went: Lobster does not know whether it was picked up, destroyed or
        teleported, and guessing would be policy. For the same reason this
        clears only `world_transform` and leaves the location alone - see
        `remove_ops`.
        """
        for op in remove_ops(item_id):
            self._write(op)
        if self.bus is not None:
            self.bus.item_removed(item_id, cell_id, transform)


# ---------------------------------------------------------------------------
# Spatial index for placed items (D38)
# ---------------------------------------------------------------------------

class ItemGrid:
    """A uniform XZ grid over one cell's placed items.

    **Why not `SpatialIndex`.** That class carries what *entities* need and
    items do not: a tier (an entity vocabulary - D18 - which items have no
    place in) and snapshot provenance (`snapshot_seq`, `snapshot_reason`,
    which exist so a stale dormant position is attributable). Items cannot go
    stale: this grid is derived from the resolution and thrown away when the
    resolution changes, so there is no staleness to attribute. Measured, that
    machinery costs 5x the build - 2,537 us against 527 for 1,000 items - to
    answer a question items never ask.

    **The traversal is shared, though**, because that is the part that has been
    wrong twice (D16's sphere bound, D31's off-by-one dilation). Both this and
    `SpatialIndex` walk through `lobster.spatial.dilated_segment_walk`, so a
    fix lands once.

    Entries are `(item_id, position)` pairs and nothing else. There is no
    `move` and no `remove`: the way an item moves is that its record changes
    and this gets rebuilt.
    """

    __slots__ = ("cell_id", "cell_size_m", "buckets", "count")

    def __init__(self, cell_id: str, records: Iterable[Mapping[str, Any]] = (),
                 *, cell_size_m: float = SPATIAL_GRID_CELL_M) -> None:
        self.cell_id = cell_id
        self.cell_size_m = float(cell_size_m)
        self.buckets: Dict[Tuple[int, int], List[Tuple[str, Vec3]]] = {}
        self.count = 0
        for record in records:
            raw = (record.get(WORLD_TRANSFORM) or {}).get("position")
            if not raw:
                continue
            position = (float(raw[0]), float(raw[1]), float(raw[2]))
            self.buckets.setdefault(
                bucket_of(position, self.cell_size_m), []).append(
                    (record["id"], position))
            self.count += 1

    def __len__(self) -> int:
        return self.count

    def near_segment(self, start: Vec3, end: Vec3,
                     radius: float) -> List[Tuple[str, Vec3]]:
        """Candidates whose bucket the segment passes within `radius` of.

        Deduplicated by item id, because the dilated walk can reach the same
        bucket from two samples. Returns positions so the caller can run the
        narrow test without touching a record again.
        """
        if not self.buckets:
            return []
        span = span_for(radius, self.cell_size_m)
        out: List[Tuple[str, Vec3]] = []
        seen: Set[str] = set()
        for key in dilated_segment_walk(start, end, self.cell_size_m, span):
            for item_id, position in self.buckets.get(key, ()):
                if item_id not in seen:
                    seen.add(item_id)
                    out.append((item_id, position))
        return out

    def walk_cost(self, start: Vec3, end: Vec3, radius: float) -> int:
        """How many bucket lookups `near_segment` would do.

        Exposed because the caller has to choose between this and a linear
        scan, and that choice should be made on the actual numbers rather than
        on a guess about ray length (D38).
        """
        import math
        span = span_for(radius, self.cell_size_m)
        dx, dz = end[0] - start[0], end[2] - start[2]
        steps = int(math.sqrt(dx * dx + dz * dz) / (self.cell_size_m * 0.5)) + 1
        return (steps + 1) * (2 * span + 1) ** 2
