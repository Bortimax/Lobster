"""The cheap contract, event half (Scope 13).

> **Events Lobster fires:** `on_enter_cell(location_id)`,
> `on_exit_cell(location_id)`, `on_hit_location(target_id, region, force,
> source_id)`, `on_structure_damaged(structure_id, chunk_indices[])`,
> `on_interact(target_id)`, `on_item_placed`/`on_item_removed(item_id, cell_id,
> transform)`.

"Fire exactly the listed Events with the stated shapes." That is the whole
module: seven frozen payload types, one bus, and one sink that forwards the
subset Octopus has real trigger types for.

L4 - "Lobster reports; it does not decide" - is the reason every payload is
pure observation. There is no `damage`, no `killed`, no `severed`; there is
`force` and a `region`, and what either means is somebody else's record.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .geometry import Transform

# ---------------------------------------------------------------------------
# Event names. These strings are the wire; nothing else may be fired.
# ---------------------------------------------------------------------------

ON_ENTER_CELL = "on_enter_cell"
ON_EXIT_CELL = "on_exit_cell"
ON_HIT_LOCATION = "on_hit_location"
ON_STRUCTURE_DAMAGED = "on_structure_damaged"
ON_INTERACT = "on_interact"
ON_ITEM_PLACED = "on_item_placed"
ON_ITEM_REMOVED = "on_item_removed"

CONTRACT_EVENTS: Tuple[str, ...] = (
    ON_ENTER_CELL, ON_EXIT_CELL, ON_HIT_LOCATION, ON_STRUCTURE_DAMAGED,
    ON_INTERACT, ON_ITEM_PLACED, ON_ITEM_REMOVED,
)


class ContractViolation(Exception):
    """An event outside the contract, or a payload of the wrong shape."""


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """Base: every payload knows its own name and serialises to plain data."""

    name: str = dc_field(init=False, default="")

    def to_dict(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True)
class OnEnterCell(Event):
    location_id: str
    name: str = dc_field(init=False, default=ON_ENTER_CELL)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "location_id": self.location_id}


@dataclass(frozen=True)
class OnExitCell(Event):
    location_id: str
    name: str = dc_field(init=False, default=ON_EXIT_CELL)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "location_id": self.location_id}


@dataclass(frozen=True)
class OnHitLocation(Event):
    """`region` is Optional - see DECISIONS.md D16 (which supersedes D5).

    A hit names one of the six humanoid regions at every tier that has a
    hitbox. A PROJECTILE-tier hit is gated by a whole-body capsule and then
    refined per-bone, so a sniper's shot resolves to a limb rather than to
    "somebody, somewhere". `region` is None only when the target has no rig.

    How precisely it was measured is on `HitResult.region_precise`, not here:
    the four fields of this Event are fixed by Scope 13 and do not grow.
    """

    target_id: str
    region: Optional[str]
    force: float
    source_id: Optional[str]
    name: str = dc_field(init=False, default=ON_HIT_LOCATION)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "target_id": self.target_id,
                "region": self.region, "force": self.force,
                "source_id": self.source_id}


@dataclass(frozen=True)
class OnStructureDamaged(Event):
    structure_id: str
    chunk_indices: Tuple[int, ...]
    name: str = dc_field(init=False, default=ON_STRUCTURE_DAMAGED)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "structure_id": self.structure_id,
                "chunk_indices": list(self.chunk_indices)}


@dataclass(frozen=True)
class OnInteract(Event):
    target_id: str
    name: str = dc_field(init=False, default=ON_INTERACT)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "target_id": self.target_id}


@dataclass(frozen=True)
class OnItemPlaced(Event):
    item_id: str
    cell_id: str
    transform: Transform
    name: str = dc_field(init=False, default=ON_ITEM_PLACED)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "item_id": self.item_id,
                "cell_id": self.cell_id,
                "transform": self.transform.to_dict()}


@dataclass(frozen=True)
class OnItemRemoved(Event):
    item_id: str
    cell_id: str
    transform: Transform
    name: str = dc_field(init=False, default=ON_ITEM_REMOVED)

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.name, "item_id": self.item_id,
                "cell_id": self.cell_id,
                "transform": self.transform.to_dict()}


PAYLOAD_TYPES = {
    ON_ENTER_CELL: OnEnterCell,
    ON_EXIT_CELL: OnExitCell,
    ON_HIT_LOCATION: OnHitLocation,
    ON_STRUCTURE_DAMAGED: OnStructureDamaged,
    ON_INTERACT: OnInteract,
    ON_ITEM_PLACED: OnItemPlaced,
    ON_ITEM_REMOVED: OnItemRemoved,
}

Listener = Callable[[Event], None]


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------

class EventBus:
    """Fan-out for the seven contract events, plus a recorded log.

    The log exists so a test (and the CLI's `events` dump) can assert on what
    was fired without a subscriber. It is bounded: `max_log` entries, oldest
    dropped, because an unbounded event log inside the shell is exactly the
    kind of shell-side growth L7 forbids.
    """

    def __init__(self, *, max_log: int = 4096) -> None:
        self._listeners: Dict[str, List[Listener]] = {}
        self._any: List[Listener] = []
        self.max_log = max_log
        self.log: List[Event] = []

    def subscribe(self, event_name: str, fn: Listener) -> None:
        if event_name not in CONTRACT_EVENTS:
            raise ContractViolation(
                "{0!r} is not a contract event; the set is frozen at "
                "{1}".format(event_name, list(CONTRACT_EVENTS)))
        self._listeners.setdefault(event_name, []).append(fn)

    def subscribe_all(self, fn: Listener) -> None:
        self._any.append(fn)

    def emit(self, event: Event) -> Event:
        expected = PAYLOAD_TYPES.get(event.name)
        if expected is None or not isinstance(event, expected):
            raise ContractViolation(
                "refusing to emit {0!r}: payload type {1} is not the declared "
                "shape for that event".format(event.name,
                                              type(event).__name__))
        self.log.append(event)
        if len(self.log) > self.max_log:
            del self.log[:len(self.log) - self.max_log]
        for fn in self._listeners.get(event.name, ()):
            fn(event)
        for fn in self._any:
            fn(event)
        return event

    # -- convenience emitters, one per contract event ------------------------
    def enter_cell(self, location_id: str) -> Event:
        return self.emit(OnEnterCell(location_id=location_id))

    def exit_cell(self, location_id: str) -> Event:
        return self.emit(OnExitCell(location_id=location_id))

    def hit_location(self, target_id: str, region: Optional[str], force: float,
                     source_id: Optional[str]) -> Event:
        return self.emit(OnHitLocation(target_id=target_id, region=region,
                                       force=float(force),
                                       source_id=source_id))

    def structure_damaged(self, structure_id: str,
                          chunk_indices: Sequence[int]) -> Event:
        return self.emit(OnStructureDamaged(
            structure_id=structure_id,
            chunk_indices=tuple(sorted({int(i) for i in chunk_indices}))))

    def interact(self, target_id: str) -> Event:
        return self.emit(OnInteract(target_id=target_id))

    def item_placed(self, item_id: str, cell_id: str,
                    transform: Transform) -> Event:
        return self.emit(OnItemPlaced(item_id=item_id, cell_id=cell_id,
                                      transform=transform))

    def item_removed(self, item_id: str, cell_id: str,
                     transform: Transform) -> Event:
        return self.emit(OnItemRemoved(item_id=item_id, cell_id=cell_id,
                                       transform=transform))

    # -- reading -------------------------------------------------------------
    def events_of(self, event_name: str) -> List[Event]:
        return [e for e in self.log if e.name == event_name]

    def dump(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.log]

    def clear(self) -> None:
        self.log.clear()


# ---------------------------------------------------------------------------
# Octopus sink
# ---------------------------------------------------------------------------

class OctopusEventSink:
    """Forwards contract events into Octopus's trigger system.

    Only the shapes Octopus can actually consume are forwarded, and only as
    *bindings* (id strings): `EventEngine._substitute` binds names to record
    ids, so a list like `chunk_indices` has no place in a binding. The full
    payload stays on Lobster's bus, where Shrimp reads it.

    Two events map onto Octopus's own trigger vocabulary rather than a new
    string:

    * `on_enter_cell` -> `GameSession.enter_scene(location_id)`, but only when
      the cell is being entered *by the player*. Octopus's D31 note says the
      engine cannot see a player walk through a door and exposes `enter_scene`
      for the consumer to say so; Lobster is that consumer. Loading a
      neighbouring cell for residency is not the player entering a scene, and
      must not fire it.
    * `on_interact` -> `GameSession.interact(target_id)`, which is Octopus's
      own `on_interact` trigger with its documented bindings.

    The rest are fired as open-string trigger types, which
    `CONTENT_FORMAT.md` 5 explicitly permits ("New `trigger_type` and
    `action_type` values need no declaration at all").
    """

    def __init__(self, session: Any) -> None:
        self.session = session
        self.fired: List[Dict[str, Any]] = []

    def attach(self, bus: EventBus) -> "OctopusEventSink":
        bus.subscribe_all(self.on_event)
        return self

    def on_event(self, event: Event) -> None:
        if event.name in (ON_ENTER_CELL, ON_EXIT_CELL):
            # **Neither** residency Event is forwarded, and the symmetry is the
            # point. CONTRACT §1 has always said loading a neighbour is not the
            # player entering a scene - and unloading one two hops away is not
            # the player leaving. Forwarding only the exit meant a Trigger bound
            # to it fired on ring churn, for cells the player was never in, and
            # several times per transition (Shrimp finding #2, D46).
            #
            # The player's own movement is announced explicitly:
            # `enter_scene()` and `exit_scene()`.
            return
        if event.name == ON_INTERACT:
            self._record(ON_INTERACT, {"target": event.target_id})
            self.session.interact(event.target_id)
            return
        if event.name == ON_HIT_LOCATION:
            bindings = {"target": event.target_id,
                        "subject": event.target_id}
            if event.source_id:
                bindings["source"] = event.source_id
        elif event.name == ON_STRUCTURE_DAMAGED:
            bindings = {"target": event.structure_id,
                        "object": event.structure_id}
        elif event.name in (ON_ITEM_PLACED, ON_ITEM_REMOVED):
            bindings = {"object": event.item_id, "location": event.cell_id}
        else:  # pragma: no cover - CONTRACT_EVENTS is exhaustive above
            return
        self._record(event.name, bindings)
        self.session.fire(event.name, **bindings)

    def enter_scene(self, location_id: str) -> None:
        """The player entered this cell. Separate from residency loading."""
        self._record("on_enter_scene", {"location": location_id})
        self.session.enter_scene(location_id)

    def exit_scene(self, location_id: str) -> None:
        """The player left this cell. The counterpart to `enter_scene`.

        Fired as `on_exit_scene`, an open-string trigger, because Octopus
        exposes `enter_scene` and has no `exit_scene` of its own. Content binds
        to it exactly as it would to any other trigger type
        (`CONTENT_FORMAT.md` §5 permits new ones).

        **Only the consumer knows this happened.** Lobster sees cells load and
        unload; it does not see a player decide to walk out of one, which is the
        same reason `enter_scene` exists rather than being inferred from
        `on_enter_cell` (Octopus D31).
        """
        self._record("on_exit_scene", {"location": location_id})
        self.session.fire("on_exit_scene", location=location_id)

    def _record(self, trigger_type: str, bindings: Dict[str, Any]) -> None:
        self.fired.append({"trigger_type": trigger_type,
                           "bindings": dict(bindings)})
