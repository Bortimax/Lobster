"""The cheap contract, query half (Scope 13).

> **Live queries Lobster is permitted to call**, all pure, never cached beyond
> the current hit-test or frame: `queries.resolve_npc_state`,
> `queries.zone_occupants`, `queries.resolve_equipment_slots`,
> `queries.list_active_effects`, the `limb_state` read (5, hit-testing only),
> and [...] `queries.structures_in_zone(zone_id)` and
> `queries.structures_in_location(location_id)`.

This module is the *only* place in Lobster that imports `lce`. Everything else
goes through `FrameView`. Three properties are enforced here rather than
documented and hoped for:

**1. The permitted set is closed.** `PERMITTED_QUERIES` is the list above and
nothing else. A test walks Lobster's source for `lce.` imports outside this
module.

**2. Nothing is cached across frames.** A `FrameView` is opened for one frame
(or one hit-test), and closes. Calling a query on a closed view raises. The one
thing that *is* carried is a derived *index*, never a query result - and only
for as long as the `Resolution` it was derived from is the current one. That is
Octopus's own precedent: `GameSession.occupancy_index` rebuilds the occupancy
index whenever the resolution changes, because it is "pure derived data, never
save-layer state".

**3. `limb_state` is read for hit-testing and nothing else.** The reader has to
pass the `HIT_TEST_ONLY` sentinel by name. Scope 15.4 names the exact failure
this guards:

> A hitbox is only correct if limb-state is read live at hit-test time - but
> don't let that turn into Lobster also driving animation from the same read
> (5/L8).

Two queries in the permitted list do not exist in Octopus yet
(`structures_in_zone`, `structures_in_location` - new in Scope v0.3). They are
implemented here as pure lookups over resolved state with the declared cost
model, and `delegates_to_octopus()` reports which ones are Octopus's own so the
day Octopus lands them, the swap is one function and a green test.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .octopus_path import ensure_lce_importable
from .tiers import octopus_tier_for

ensure_lce_importable()

from lce import queries as _q  # noqa: E402
from lce.resolver import Resolution  # noqa: E402
from lce.zones import OccupancyIndex, build_occupancy_index  # noqa: E402

#: The closed set. Adding a name here is a Scope change, not an implementation
#: detail (Scope 13 lists these exactly).
PERMITTED_QUERIES: Tuple[str, ...] = (
    "resolve_npc_state",
    "zone_occupants",
    "resolve_equipment_slots",
    "list_active_effects",
    "limb_state",
    "structures_in_zone",
    "structures_in_location",
)

#: Which of them are Octopus's own functions today.
_OCTOPUS_OWNED = ("resolve_npc_state", "zone_occupants",
                  "resolve_equipment_slots", "list_active_effects")

#: Record type Lobster's schema extension declares for break-state.
STRUCTURE_STATE_TYPE = "StructureState"

#: Field on Character holding the live limb map (Scope 5).
LIMB_STATE_FIELD = "limb_state"

LIMB_INTACT = "intact"
LIMB_DISABLED = "disabled"
LIMB_SEVERED = "severed"
LIMB_STATES = (LIMB_INTACT, LIMB_DISABLED, LIMB_SEVERED)


class ContractError(Exception):
    """A use of the Octopus surface the contract does not permit."""


class _HitTestOnly:
    """Sentinel. Its only job is to make the caller write the words."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "HIT_TEST_ONLY"


HIT_TEST_ONLY = _HitTestOnly()


def delegates_to_octopus() -> Dict[str, bool]:
    """Which permitted queries currently call Octopus code directly."""
    return {name: name in _OCTOPUS_OWNED for name in PERMITTED_QUERIES}


# ---------------------------------------------------------------------------
# Derived index for the two structure queries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StructureIndex:
    """location_id -> StructureState records in it.

    Pure derived data over a `Resolution`, exactly like Octopus's occupancy
    index: rebuilt when the resolution changes, never persisted, never a source
    of truth. It exists so `structures_in_location` costs O(structures in that
    location) rather than O(structures in the world), which is the cost model
    Scope 13 declares for it.
    """

    by_location: Dict[str, Tuple[Dict[str, Any], ...]] = dc_field(
        default_factory=dict)
    records_scanned_at_build: int = 0

    def in_location(self, location_id: str) -> List[Dict[str, Any]]:
        return list(self.by_location.get(location_id, ()))


def build_structure_index(resolution: Resolution) -> StructureIndex:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    scanned = 0
    for rec in resolution.by_type(STRUCTURE_STATE_TYPE):
        scanned += 1
        loc = rec.get("location_id")
        if isinstance(loc, str) and loc:
            buckets.setdefault(loc, []).append(rec)
    return StructureIndex(
        by_location={k: tuple(sorted(v, key=lambda r: r["id"]))
                     for k, v in buckets.items()},
        records_scanned_at_build=scanned)


# ---------------------------------------------------------------------------
# Frame view
# ---------------------------------------------------------------------------

class FrameView:
    """One frame's (or one hit-test's) permitted read access to Octopus.

    Holds a `Resolution` and a tick and nothing else derived from a query.
    Closing it makes every further call raise, which is how "never cached
    beyond the current hit-test or frame" stops being a comment.
    """

    def __init__(self, resolution: Resolution, tick: int, *,
                 occupancy: Optional[OccupancyIndex] = None,
                 structures: Optional[StructureIndex] = None,
                 player_id: Optional[str] = None) -> None:
        self._resolution = resolution
        self.tick = int(tick)
        self.player_id = player_id
        self._occupancy = occupancy
        self._structures = structures
        self._open = True
        #: every permitted call made through this view, for the CLI dump and
        #: for the contract test that asserts nothing else was called.
        self.calls: List[str] = []

    # -- lifecycle -----------------------------------------------------------
    @property
    def open(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False

    def __enter__(self) -> "FrameView":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _live(self, name: str) -> Resolution:
        if not self._open:
            raise ContractError(
                "{0}: this FrameView is closed. Octopus queries are pure and "
                "must be re-read, never held across a frame or a hit-test "
                "(Scope 13).".format(name))
        self.calls.append(name)
        return self._resolution

    # -- the seven permitted queries ----------------------------------------
    def resolve_npc_state(self, npc_id: str, *, lobster_tier: str,
                          **kwargs: Any) -> Dict[str, Any]:
        """Octopus's own `resolve_npc_state`, with the tier mapping enforced.

        The caller passes *Lobster's* tier; `octopus_tier_for` decides what
        Octopus is told. PROJECTILE never reaches Octopus, so a hit-test
        candidate is never promoted to active simulation (Scope 7).
        """
        res = self._live("resolve_npc_state")
        return _q.resolve_npc_state(res, npc_id, self.tick,
                                    tier=octopus_tier_for(lobster_tier),
                                    player_id=self.player_id, **kwargs)

    def zone_occupants(self, zone_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
        res = self._live("zone_occupants")
        return _q.zone_occupants(res, self.occupancy(), zone_id, self.tick,
                                 player_id=self.player_id, **kwargs)

    def resolve_equipment_slots(self, character_id: str, **kwargs: Any) -> Any:
        res = self._live("resolve_equipment_slots")
        return _q.resolve_equipment_slots(res, character_id, **kwargs)

    def list_active_effects(self, entity_id: str, **kwargs: Any) -> Any:
        res = self._live("list_active_effects")
        kwargs.setdefault("tick", self.tick)
        return _q.list_active_effects(res, entity_id, **kwargs)

    def limb_state(self, entity_id: str, *, purpose: Any) -> Dict[str, str]:
        """The one live limb-state read (Scope 5).

        `purpose` must be `HIT_TEST_ONLY`. There is no other legitimate caller
        inside Lobster: reading this to choose a pose or a clip is the animation
        controller's job, and L8 says the instant Lobster does that it has
        quietly become Shrimp.

        An absent record, or a Character with no `limb_state` field, resolves to
        an empty map, which every caller reads as "all limbs intact". A severed
        limb is stated, never inferred (DECISIONS.md D2).
        """
        if purpose is not HIT_TEST_ONLY:
            raise ContractError(
                "limb_state may only be read to decide which hitboxes exist "
                "for the next hit-test (Scope 5 / L8). Pass "
                "purpose=HIT_TEST_ONLY, or - if this is an animation decision "
                "- read the field from Shrimp's controller instead; both "
                "systems read the same field independently, and that is the "
                "correct boundary.")
        res = self._live("limb_state")
        rec = res.get(entity_id)
        if rec is None:
            return {}
        raw = rec.get(LIMB_STATE_FIELD)
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def structures_in_location(self, location_id: str) -> List[Dict[str, Any]]:
        """Every StructureState in one cell. O(structures in that location)."""
        self._live("structures_in_location")
        return self.structure_index().in_location(location_id)

    def structures_in_zone(self, zone_id: str) -> List[Dict[str, Any]]:
        """Every StructureState in a Zone, including descendant Zones.

        Uses Octopus's own zone->locations map from the occupancy index, so the
        Zone hierarchy is resolved by Octopus, not re-derived here.
        """
        self._live("structures_in_zone")
        index = self.structure_index()
        locations = self.occupancy().zone_locations.get(zone_id, set())
        out: List[Dict[str, Any]] = []
        for loc in sorted(locations):
            out.extend(index.in_location(loc))
        return out

    # -- derived indices (not query results) ---------------------------------
    def occupancy(self) -> OccupancyIndex:
        if self._occupancy is None:
            self._occupancy = build_occupancy_index(self._resolution)
        return self._occupancy

    def structure_index(self) -> StructureIndex:
        if self._structures is None:
            self._structures = build_structure_index(self._resolution)
        return self._structures

    def connections(self, location_id: str) -> List[Tuple[str, float]]:
        """Passable outgoing edges of a Location, as (target_id, travel_cost).

        Octopus's `lce.graph.neighbors`, which is a pure read of
        `Location.connections` - the same record read `record()` performs, with
        the entry shape already unpacked. It is not a new entry on the Scope 13
        query list and is not treated as one: residency and connection-severing
        both need the connection graph, and re-deriving adjacency in Lobster
        would be a second source of truth for it (DECISIONS.md D9).
        """
        from lce.graph import neighbors
        res = self._live("connections")
        return neighbors(res, location_id)

    def connection_path(self, from_id: str,
                        to_id: str) -> Optional[List[str]]:
        """Octopus's own answer to "is there a route", via `lce.graph`.

        Same standing as `connections`: a pure traversal of the same record
        field. The section 14 agreement test compares this against the navmesh,
        and it has to be Octopus's real traversal for that comparison to mean
        anything.
        """
        from lce.graph import shortest_path
        res = self._live("connection_path")
        return shortest_path(res, from_id, to_id)

    def records_of_type(self, type_name: str) -> List[Dict[str, Any]]:
        """Every resolved record of a type, sorted by id.

        `Resolution.by_type`, routed through the frame view. Same standing as
        `record`: a read of resolved state, not a Scope 13 query. The build step
        lints with it; the runtime does not use it, because a per-frame walk of
        every record of a type is exactly the cost the spatial index and the
        occupancy index exist to avoid.
        """
        res = self._live("records_of_type")
        return res.by_type(type_name)

    def record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Direct record read.

        Not a "query" in the Scope 13 sense - it is `Resolution.get`, the same
        thing every Octopus query is built on - but it is routed through the
        frame view so the closed-view rule applies to it too.
        """
        res = self._live("record")
        return res.get(record_id)


class OctopusBridge:
    """Opens frame views, and owns the per-resolution derived indices.

    The indices are rebuilt whenever the `Resolution` identity changes, which is
    Octopus's own pattern (`GameSession.occupancy_index`). Query *results* are
    never held.
    """

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self._index_for: Optional[int] = None
        self._occupancy: Optional[OccupancyIndex] = None
        self._structures: Optional[StructureIndex] = None
        self._open_view: Optional[FrameView] = None

    # -- resolution/tick sourcing -------------------------------------------
    def resolution(self) -> Resolution:
        if self.session is None:
            raise ContractError("OctopusBridge has no session; pass a "
                                "Resolution to frame() explicitly")
        return self.session.resolution()

    def tick(self) -> int:
        return int(getattr(self.session, "tick", 0) or 0)

    def player_id(self) -> Optional[str]:
        return getattr(self.session, "player_id", None)

    # -- frames --------------------------------------------------------------
    def frame(self, resolution: Optional[Resolution] = None,
              tick: Optional[int] = None) -> FrameView:
        """Open a view for this frame. Closes any previously open one.

        Closing the previous view is deliberate: two live views would let a
        caller read this frame's world through last frame's handle, which is
        exactly the staleness the "never cached" rule exists to prevent.
        """
        res = resolution if resolution is not None else self.resolution()
        if self._index_for != id(res):
            self._occupancy = None
            self._structures = None
            self._index_for = id(res)
        if self._open_view is not None:
            self._open_view.close()
        view = FrameView(
            res, tick if tick is not None else self.tick(),
            occupancy=self._occupancy, structures=self._structures,
            player_id=self.player_id())
        self._open_view = view
        return view

    def close_frame(self) -> None:
        if self._open_view is not None:
            # keep whatever indices the view built - they are derived from the
            # same resolution and stay valid until it changes
            self._occupancy = self._open_view._occupancy
            self._structures = self._open_view._structures
            self._open_view.close()
            self._open_view = None


# ---------------------------------------------------------------------------
# Build-step entry points
# ---------------------------------------------------------------------------
#
# The build step needs to resolve a content stack and run Octopus's own content
# lint. Both go through this module for the same reason everything else does:
# `lce` has exactly one door into Lobster, and a test asserts it. Putting these
# in `lobster.build` would have meant a second import site and a weaker rule.

def content_view(package_paths: Sequence[str], *, tick: int = 0,
                 include_kinds: Tuple[str, ...] = ("standard",)) -> FrameView:
    """Resolve a content stack with **no save layer**, for the build step.

    A build sees content only. Break-state written by a playthrough is not the
    build's business, and resolving a save here would let a player's damage
    change what gets baked - which is precisely the direction of dependency
    Scope 6 forbids ("the authored grid [...] ships in the bundle, never saved").
    """
    from lce.catalog import build_day_one_schema
    from lce.package import build_stack, load_package
    from lce.resolver import resolve_stack

    schema = build_day_one_schema()
    packages = [load_package(path) for path in package_paths]
    result = build_stack(packages, schema, include_kinds=include_kinds)
    return FrameView(resolve_stack(schema, result.stack), tick)


def octopus_lint(view: FrameView) -> List[Dict[str, Any]]:
    """Octopus's own content lint (`lce.lint`).

    Reused rather than reimplemented: the one-way-connection check the Scope
    asks the build step to run (4, 15.2) already exists there as D26, and a
    second copy of it in Lobster would be a second thing to keep correct.
    """
    from lce.lint import lint
    return lint(view._live("lint"))


def limb_has_hitbox(state: Optional[str]) -> bool:
    """Does a limb in this state offer a hitbox to the next hit-test?

    Only `severed` removes the hitbox. `disabled` is a gameplay state - a
    disabled arm is still an arm in the world, and deciding otherwise would be
    Lobster interpreting damage, which Scope 5 gives to Octopus's stats module
    and Shrimp's content.
    """
    return state != LIMB_SEVERED
