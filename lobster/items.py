"""Placing and removing an item's representation in the world (Scope 8).

> | **Lobster** | Object selection/raycasting, world-space labels, physically
> placing/removing an item's representation in the world. |
> | **Octopus** | Inventory data - `Item` records, `resolve_equipment_slots`. |

So this module moves *where a thing is*, and knows nothing about what it is.
There is no pickup, no inventory, no weight, no stack count, no "can the player
reach it" - all of that is Shrimp's and Octopus's (L4, L8). What Lobster owns is
the fact that a sword is lying at a particular spot in a particular cell, and
the two Events that say so.

**Both fields move together.** CONTRACT §2 declares `Item.world_transform` and
`Item.current_location_ref` co-null: an item is in the world with both, or out
of it with neither. Octopus has no multi-field write, so this is maintained by
*ordering* rather than by atomicity - see `place_ops`, where every intermediate
state is "not in the world" and the location write is the commit point.

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

from .events import EventBus
from .geometry import Transform

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

        That query already filters out half-placed items, so this asserts the
        invariant rather than tolerating a breach: reaching here with a missing
        half means something bypassed both the lint and the index.
        """
        transform = record.get(WORLD_TRANSFORM)
        cell_id = record.get(LOCATION_REF)
        if not transform or not cell_id:
            raise ItemError(
                "{0!r} is half-placed: world_transform={1!r}, "
                "current_location_ref={2!r}. CONTRACT §2 declares these "
                "co-null - both, or neither".format(
                    record.get("id"), transform, cell_id))
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

    Removal runs the same argument backwards: clear the location first, and the
    item is gone from the index before its transform is cleared.

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
    """Take it back out. Location first, so it leaves the index immediately."""
    return [{"op": "PATCH", "id": item_id, "field": LOCATION_REF,
             "value": None},
            {"op": "PATCH", "id": item_id, "field": WORLD_TRANSFORM,
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
        """Take it back out, reporting where it was.

        `on_item_removed` carries the cell and transform it *had*, not where it
        went: Lobster does not know whether it was picked up, destroyed or
        teleported, and guessing would be policy.
        """
        for op in remove_ops(item_id):
            self._write(op)
        if self.bus is not None:
            self.bus.item_removed(item_id, cell_id, transform)
