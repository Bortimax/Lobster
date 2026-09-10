"""Tiered hit-testing (Scope 5, 6.5, 7).

Spatial query first, detail second:

> every entity's position lives in a per-cell spatial index; a swing or blast
> queries it for a small candidate set before any detailed hitbox test runs.

Then fidelity by tier:

    ACTIVE      full skeleton, 6-region hit test
    PROJECTILE  whole-body capsule to decide IF, then 6-region to decide WHERE
    DORMANT     none - effects reach these through 6.5's occupancy + Event path

The PROJECTILE row is a deliberate, authorised departure from the Scope 7 table,
which says "single whole-body capsule". DECISIONS.md D16 records it in full: a
sniper's shot resolving to a limb is the point of having limbs, and the
refinement is gated behind the body-capsule test, so a *miss* costs exactly what
it cost before and only a landed hit pays for the detail. The load-bearing half
of Scope 7 - that PROJECTILE is a hit-test-candidacy tier and never promotes
simulation - is untouched.

This module is the **only** place in Lobster that reads `limb_state`, and it
reads it once per target per hit-test, live, immediately before deciding which
capsules exist. Scope 15.4:

> A hitbox is only correct if limb-state is read live at hit-test time - but
> don't let that turn into Lobster also driving animation from the same read.

So the read goes to `Skeleton.hitboxes(limb_state=...)` and nowhere else. It is
not stored on the skeleton, not remembered between frames, not turned into a
pose change, and not handed to anybody. Shrimp's animation controller reads the
same field itself, independently, through the same query - and that is the
correct boundary, not an accident of layering.

What comes out is a report: target, region, force, source. Nothing here knows
what a hit *means*. `force` is what the caller measured; whether it kills, maims
or bounces off armour is Octopus's stats module and Shrimp's content (Scope 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Tuple)

from .budgets import BudgetViolation
from .constants import (HIT_TEST_FRAME_BUDGET_US,
                        PER_BUCKET_SCAN_US, PER_CANDIDATE_US,
                        PER_QUERY_US,
                        PER_CAPSULE_TEST_US,
                        MAX_BROAD_CANDIDATES_PER_FRAME,
                        MAX_BUCKET_SCANS_PER_FRAME,
                        MAX_CAPSULE_TESTS_PER_FRAME,
                        modelled_cost_us,
                        PROJECTILE_BROAD_RADIUS_M)
from .events import EventBus
from .geometry import (Capsule, Vec3, capsules_overlap, distance, normalize,
                       ray_capsule_hit, segment_segment_distance)
from .octopus_bridge import HIT_TEST_ONLY
from .skeleton import Skeleton
from .spatial import Candidate, SpatialIndex
from .tiers import ACTIVE, DORMANT, PROJECTILE, FIDELITY


class HitTestError(Exception):
    pass


def _require_rig(skeletons: Mapping[str, Skeleton], cand: Candidate,
                 cell_id: str) -> Skeleton:
    """The rig for an entity at a tier that promises a hitbox.

    ACTIVE and PROJECTILE both declare a hitbox (`tiers.has_hitbox`), so an
    entity sitting at one of them with no registered rig is an integration bug -
    and the way it used to present was the worst possible one: arrows passed
    straight through, silently, with no error anywhere.

    DORMANT is the tier for "present in the world, cannot be hit". It needs no
    rig, costs nothing, and is the right home for a mob nobody is fighting. So
    the fix is always one of two things, and the message says both.
    """
    skeleton = skeletons.get(cand.entity_id)
    if skeleton is None:
        raise HitTestError(
            "cell {0!r}: {1!r} is at tier {2}, which declares a hitbox, but no "
            "rig is registered for it - so it would be silently unhittable. "
            "Either register a Skeleton for it, or place it at {3} tier, which "
            "is what \"present in the world but not currently a hit-test "
            "candidate\" means. A tier is not an entity class: it is how "
            "precisely this entity can be hit right now.".format(
                cell_id, cand.entity_id, cand.tier, DORMANT))
    return skeleton


@dataclass
class HitTestStats:
    """What hit-testing actually cost this frame.

    Scope 7 promises "cost scales with attacks and tiers, not population", and
    Scope 15.8 says the trigger for reconsidering the uniform grid is profiling
    a real cell rather than a hunch. Neither is checkable without numbers, so
    these are the numbers.
    """

    projectiles: int = 0
    swings: int = 0
    blasts: int = 0
    body_tests: int = 0
    bone_refinements: int = 0
    capsule_tests: int = 0

    def reset(self) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, 0)

    def to_dict(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class HitResult:
    """One resolved hit. The payload of `on_hit_location`, plus provenance.

    `region` names one of the six regions whenever a rig was available to
    measure against - at **both** ACTIVE and PROJECTILE tier. See DECISIONS.md
    D16, which supersedes D5: a projectile that lands is worth resolving to a
    limb, and measuring it costs a few microseconds per landed hit.

    `region_precise` says how it was measured, and it is the honest half:

    * `True`  - the ray intersected that bone's capsule.
    * `False` - the body capsule was hit but no bone capsule was (a graze along
      the silhouette, or a stale pose on a distant target), so `region` is the
      *nearest* bone. A blast also reports False: there is no ray to trace, so
      the region is the one nearest the epicentre.

    `region` is None only when the target has no rig at all.

    `snapshot_seq` / `snapshot_reason` carry how fresh the *position* was;
    `pose_version` carries how fresh the *pose* that resolved the region was. A
    PROJECTILE-tier target is by definition one nobody is animating closely, so
    a consumer that cares can see that rather than assume it was live.
    """

    target_id: str
    region: Optional[str]
    force: float
    source_id: Optional[str]
    tier: str
    snapshot_seq: int
    snapshot_reason: str
    region_precise: bool = False
    pose_version: int = 0
    #: which cell the target was resolved in. A shot may cross cells, so a
    #: caller that wants to look the target up has to be told where it was.
    cell_id: Optional[str] = None
    #: metres from the shooter to the target, in world space. What orders
    #: several hits when a single shot crosses a cell boundary.
    distance_from_source: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"target_id": self.target_id, "region": self.region,
                "force": self.force, "source_id": self.source_id,
                "tier": self.tier, "snapshot_seq": self.snapshot_seq,
                "snapshot_reason": self.snapshot_reason,
                "region_precise": self.region_precise,
                "pose_version": self.pose_version, "cell_id": self.cell_id,
                "distance_from_source": self.distance_from_source}


# ---------------------------------------------------------------------------
# Seam kernel (DECISIONS.md D26/D28)
# ---------------------------------------------------------------------------

def _candidate_from(entry: Any, distance_m: float) -> Any:
    """A `Candidate` rebuilt from an index `Entry` plus a measured distance.

    The volley path gets ids and distances back from a kernel rather than
    `Candidate` objects, and `_report` wants the snapshot provenance the entry
    carries - which is the whole point of that provenance existing (Scope
    §15.10).
    """
    from .spatial import Candidate
    return Candidate(entity_id=entry.entity_id, position=entry.position,
                     tier=entry.tier, snapshot_seq=entry.snapshot_seq,
                     snapshot_reason=entry.snapshot_reason,
                     distance=distance_m)


def nearest_region(boxes: Sequence[Tuple[str, Capsule]], origin: Vec3,
                   direction: Vec3, max_distance: float
                   ) -> Tuple[Optional[str], bool]:
    """Which region a ray struck, over an already-filtered capsule list.

    **This is one of the two accelerator seam kernels** (D26), and it is a
    module-level pure function rather than a method body so that a native
    implementation can replace exactly this and nothing else.

    It sits **below the limb-state read on purpose.** The caller does the live
    `limb_state` query and hands over only the capsules that survived it, so a
    native kernel never touches limb state and the L8 boundary stays in Python
    permanently, where `tests/test_limb_state.py` can still see it.

    Two properties any replacement must preserve, both load-bearing and neither
    obvious from the signature:

    1. **Ties go to the earlier capsule.** The comparison is strictly `<`, so
       when two bones are equidistant the one earlier in `boxes` wins. Ordering
       is therefore part of the input, not an incidental detail.
    2. **`precise=False` is a measurement, not a fallback.** Every capsule is
       tested and the minimum gap taken, so the nearest bone is reported even
       when none was intersected.
    """
    best_hit: Optional[Tuple[float, str]] = None
    best_near: Optional[Tuple[float, str]] = None
    # Normalised once, so "did it strike" and "what was nearest" measure the
    # same reach. They did not: `ray_capsule_hit` normalises internally while
    # this line used the raw direction, so a caller passing a direction of
    # length 4 got `precise` judged over `max_distance` metres and `region`
    # judged over four times that. Both cannot be right, and the disagreement
    # was invisible because every caller in the tree happens to pass a unit
    # vector - found by differential-testing a second implementation, which is
    # what D28 exists for (D41).
    heading = normalize(direction)
    far = tuple(origin[i] + heading[i] * max_distance for i in range(3))
    for region, capsule in boxes:
        centre = tuple((capsule.a[i] + capsule.b[i]) * 0.5 for i in range(3))
        along = distance(origin, centre)
        if ray_capsule_hit(origin, heading, capsule, max_distance):
            if best_hit is None or along < best_hit[0]:
                best_hit = (along, region)
        gap = segment_segment_distance(origin, far, capsule.a, capsule.b)
        if best_near is None or gap < best_near[0]:
            best_near = (gap, region)

    if best_hit is not None:
        return best_hit[1], True
    return (best_near[1] if best_near else None), False


class HitTester:
    """Resolves swings, projectiles and blasts against one cell's index.

    Holds no state between calls except the objects it was given. Every
    `limb_state` read happens inside the `FrameView` the caller passes in, which
    is closed at the end of the frame - so "never cached beyond the current
    hit-test or frame" is enforced by the view, not promised by this class.
    """

    def __init__(self, index: SpatialIndex,
                 skeletons: Mapping[str, Skeleton],
                 bus: Optional[EventBus] = None, *,
                 max_capsule_tests_per_frame: int =
                 MAX_CAPSULE_TESTS_PER_FRAME,
                 max_broad_candidates_per_frame: int =
                 MAX_BROAD_CANDIDATES_PER_FRAME,
                 max_bucket_scans_per_frame: int =
                 MAX_BUCKET_SCANS_PER_FRAME,
                 frame_budget_us: float = HIT_TEST_FRAME_BUDGET_US) -> None:
        self.index = index
        self.skeletons = skeletons
        self.bus = bus
        self.max_capsule_tests_per_frame = max_capsule_tests_per_frame
        self.max_broad_candidates_per_frame = max_broad_candidates_per_frame
        self.max_bucket_scans_per_frame = max_bucket_scans_per_frame
        self.frame_budget_us = frame_budget_us
        self.stats = HitTestStats()
        self._margin: Optional[float] = None
        self._margin_for: int = -1

    # -- broad-phase margin --------------------------------------------------
    def broad_margin(self) -> float:
        """How far past a query volume the broad phase has to look.

        A spatial query measures to an entity's **position**, which is at its
        feet. A rig occupies space above and around that point, so any gating
        volume has to be widened by the rig's own extent or it culls hits before
        a single capsule is tested. Both halves of that were wrong once:

        * a chest-height sword swing found nothing, because the swing sphere was
          `half-length + swing radius` from a point 1.2 m below the blade -
          melee only connected at ankle height;
        * `PROJECTILE_BROAD_RADIUS_M = 2.5` was documented as "1.8 m humanoid +
          margin", which is a *height* argument for a radius, and would have
          culled a 6 m wyrm at broad phase for the same reason.

        So it is derived from the rigs actually registered, floored at the
        declared constant. It can only ever be too large, which costs a few
        extra candidates; too small silently loses hits. Recomputed when the rig
        count changes; call `refresh_bounds()` after swapping a rig in place.

        See DECISIONS.md D19.
        """
        if self._margin is None or self._margin_for != len(self.skeletons):
            widest = max((s.bound_radius() for s in self.skeletons.values()),
                         default=0.0)
            self._margin = max(PROJECTILE_BROAD_RADIUS_M, widest)
            self._margin_for = len(self.skeletons)
        return self._margin

    def refresh_bounds(self) -> float:
        """Force the broad margin to be re-derived from the current rigs."""
        self._margin = None
        return self.broad_margin()

    def begin_frame(self) -> None:
        """Reset the per-frame counters. The ceilings are checked against them.

        Resets the spatial index's query counters too: the index is per-cell and
        this is that cell's frame, so both halves of the cost - broad candidates
        and detailed capsules - are counted over the same window.

        A caller that never calls this gets no ceiling, which is right for a
        one-shot test or a tool; a game loop calls it every frame and finds out
        loudly when an engagement outgrows what the shell promised (L6).
        """
        self.stats.reset()
        self.index.stats.queries = 0
        self.index.stats.buckets_scanned = 0
        self.index.stats.buckets_visited = 0
        self.index.stats.candidates_considered = 0

    def _charge(self) -> None:
        """Check the frame budget, then the unit ceilings. Names the driver.

        **The budget is time, modelled from counters** - never read off a
        clock, so the same frame passes or fails identically on every machine
        (Scope 13: failure must be visible and attributable, which includes
        reproducible). The unit ceilings below are secondary: each is the point
        where that unit alone would spend the slice, so under mixed load this
        check fires first.

        The previous version charged only broad candidates and capsule tests,
        and `buckets_visited` counted only the buckets that held somebody. A
        long shot across a thin crowd was therefore charged almost nothing for
        most of its cost, and the ceiling sat at ~196 ms of wall time - it
        bounded the algorithm's scaling and not the frame. See DECISIONS.md D27.
        """
        queries = self.index.stats.queries
        scans = self.index.stats.buckets_scanned
        broad = self.index.stats.candidates_considered
        capsules = self.stats.capsule_tests
        attacks = "{0} projectiles, {1} swings, {2} blasts this frame".format(
            self.stats.projectiles, self.stats.swings, self.stats.blasts)
        crowd_advice = (
            "an engagement this size is a mass-casualty event and belongs on "
            "the zone-occupancy path (Scope 6.5/7), resolved once, rather than "
            "as individual ray tests")
        # Attribution is only useful if the remedy matches the driver. A volley
        # into a crowd and four long shots across an empty cell blow the same
        # budget for opposite reasons, and "use zone occupancy" is nonsense
        # advice for the second.
        traversal_advice = (
            "this is grid-walk cost, not crowd cost - it scales with ray "
            "length times queries, not with how many bodies are near the "
            "path. Shorten the segment resolved per frame, or run the volley "
            "through the accelerator seam (DECISIONS.md D26)")
        advice = crowd_advice

        if self.frame_budget_us:
            cost = modelled_cost_us(queries, scans, broad, capsules)
            if cost > self.frame_budget_us:
                parts = (("query setup", queries * PER_QUERY_US),
                         ("grid traversal", scans * PER_BUCKET_SCAN_US),
                         ("broad candidates", broad * PER_CANDIDATE_US),
                         ("capsule tests", capsules * PER_CAPSULE_TEST_US))
                driver = max(parts, key=lambda kv: kv[1])
                why = (traversal_advice if driver[0] == "grid traversal"
                       else crowd_advice)
                raise BudgetViolation(
                    cell_id=self.index.cell_id, record_id=None,
                    metric="hit_test_frame_us", value=int(cost),
                    limit=int(self.frame_budget_us),
                    detail="{0}; {1} us of it is {2} ({3} scans, {4} "
                           "candidates, {5} capsule tests) - {6}".format(
                               attacks, int(driver[1]), driver[0],
                               scans, broad, capsules, why))

        for value, limit, metric in (
                (scans, self.max_bucket_scans_per_frame,
                 "bucket_scans_per_frame"),
                (broad, self.max_broad_candidates_per_frame,
                 "broad_candidates_per_frame"),
                (capsules, self.max_capsule_tests_per_frame,
                 "capsule_tests_per_frame")):
            if limit and value > limit:
                raise BudgetViolation(
                    cell_id=self.index.cell_id, record_id=None, metric=metric,
                    value=value, limit=limit,
                    detail="{0} - {1}".format(attacks, advice))

    # -- melee ---------------------------------------------------------------
    def resolve_swing(self, view: Any, swing: Capsule, force: float, *,
                      source_id: Optional[str] = None,
                      max_regions_per_target: int = 1) -> List[HitResult]:
        """A swing volume against ACTIVE-tier entities only.

        ACTIVE is "current cell + immediate melee range" (Scope 7), so a swing
        has nothing to say about the other tiers by construction - not as an
        optimisation, but because a sword does not reach them.
        """
        self.stats.swings += 1
        mid = tuple((swing.a[i] + swing.b[i]) * 0.5 for i in range(3))
        # + broad_margin, or a swing above ankle height finds nobody: the query
        # measures to an entity's feet, and a chest-height blade is over a metre
        # away from those. The narrow phase below still tests the real capsule.
        reach = (distance(swing.a, swing.b) * 0.5 + swing.radius
                 + self.broad_margin())
        candidates = self.index.query_sphere(mid, reach, tiers=(ACTIVE,))
        out: List[HitResult] = []
        for cand in candidates:
            skeleton = _require_rig(self.skeletons, cand, self.index.cell_id)
            for region in self._regions_hit(view, cand, skeleton, swing,
                                            max_regions_per_target):
                out.append(self._report(cand, region, force, source_id,
                                        region_precise=True,
                                        pose_version=skeleton.pose_version))
        self._charge()
        return out

    def _regions_hit(self, view: Any, cand: Candidate, skeleton: Skeleton,
                     swing: Capsule, limit: int) -> List[str]:
        """The live limb-state read. The only one in Lobster."""
        limb_state = view.limb_state(cand.entity_id, purpose=HIT_TEST_ONLY)
        boxes = skeleton.hitboxes(limb_state=limb_state)
        self.stats.bone_refinements += 1
        self.stats.capsule_tests += len(boxes)
        hits: List[Tuple[float, str]] = []
        for region, capsule in boxes:
            if capsules_overlap(swing, capsule):
                centre = tuple((capsule.a[i] + capsule.b[i]) * 0.5
                               for i in range(3))
                hits.append((distance(swing.a, centre), region))
        hits.sort()
        return [region for _, region in hits[:max(1, limit)]]

    # -- projectiles ---------------------------------------------------------
    def resolve_projectile(self, view: Any, origin: Vec3, direction: Vec3,
                           max_distance: float, force: float, *,
                           source_id: Optional[str] = None,
                           first_hit_only: bool = True) -> List[HitResult]:
        """A projectile against ACTIVE and PROJECTILE tiers.

        The distinction Scope 7 insists on: an entity Octopus considers dormant
        may still be a *geometry candidate* here. Being hit does not promote it
        - nothing in this path calls Octopus with an ACTIVE tier, and the effect
        that follows reaches it through the ordinary Event path, exactly as an
        unwitnessed world-tick event would.
        """
        self.stats.projectiles += 1
        end = tuple(origin[i] + direction[i] * max_distance for i in range(3))
        # Measured to an entity's position, which is at its feet - so it has to
        # span whatever the rig occupies, or a headshot is culled before a
        # single capsule is tested. Derived from the registered rigs, not from
        # an assumed humanoid height.
        candidates = self.index.query_segment(
            origin, end, radius=self.broad_margin(),
            tiers=(ACTIVE, PROJECTILE))
        out: List[HitResult] = []
        for cand in candidates:
            skeleton = _require_rig(self.skeletons, cand, self.index.cell_id)

            if cand.tier != ACTIVE:
                # Broad phase for a distant target: one capsule test decides
                # whether it was hit at all. A miss costs 3.5us and stops here,
                # which is what keeps a volley proportional to arrows rather
                # than to army size.
                self.stats.body_tests += 1
                body = skeleton.whole_body_capsule()
                if not ray_capsule_hit(origin, direction, body, max_distance):
                    continue

            region, precise = self._region_for_ray(
                view, cand.entity_id, skeleton, origin, direction, max_distance)

            if cand.tier == ACTIVE and not precise:
                # Close range, full skeleton, nothing intersected: that is a
                # miss. The body capsule never gated this test, so there is no
                # earlier finding to fall back on.
                continue
            if region is None:
                continue
            out.append(self._report(cand, region, force, source_id,
                                    region_precise=precise,
                                    pose_version=skeleton.pose_version))
            if first_hit_only and out:
                break
        self._charge()
        return out


    def resolve_volley(self, view: Any, shots: Sequence[Any], *,
                       first_hit_only: bool = True) -> List[List["HitResult"]]:
        """A whole volley in one call. **Equivalent to N `resolve_projectile`s.**

        `shots` are `(origin, direction, max_distance, force, source_id)`
        tuples; the result is one list of hits per shot, in the same order. A
        test asserts that equivalence directly, because a faster path that
        answers differently is not a faster path.

        This is the granularity D26 fixed the accelerator seam at, and the
        reason is Amdahl: a per-arrow kernel wins the broad phase and hands the
        win back in dispatch. Three things are shared across the volley and
        cannot be shared across separate calls:

        1. **The entity table is built once.** Packing the spatial index into a
           payload is O(entities); doing it per arrow made it O(arrows x
           entities), which is precisely the population scaling Scope 7 exists
           to avoid.
        2. **Each rig's hitboxes are resolved once.** `limb_state` is read once
           per entity per volley instead of once per entity per arrow. §13
           permits exactly this - *"never cached beyond the current hit-test or
           frame"* - and a volley is one frame's worth of hit-testing. A limb
           severed *by* this volley is not visible to it, which is already true
           of the per-arrow path: `resolve_projectile` reports, it does not
           apply damage (L4).
        3. **The budget is charged once**, against the accumulated counters.

        The kernels come from `lobster.accel`, so this runs on C where the
        extension is built, on NumPy where it is not, and on the reference
        where neither is - with the same answers either way (D28).
        """
        from .accel import select as _select_accel
        _name, kernels = _select_accel()
        segment_query = kernels["segment_query"]
        nearest = kernels["nearest_region"]
        # Selected here and handed down, not looked up per rig: `select()`
        # costs more than the kernel does for a six-bone humanoid, so a lookup
        # inside `hitbox_rows` would hand the win back (D54).
        place = kernels["pose_capsules"]

        entries = self._volley_entries()
        radius = self.broad_margin()
        boxes_of: Dict[str, Any] = {}
        out: List[List[HitResult]] = []

        for shot in shots:
            origin, direction, max_distance, force, source_id = shot
            self.stats.projectiles += 1
            end = tuple(origin[i] + direction[i] * max_distance
                        for i in range(3))
            found = segment_query({
                "entries": entries, "start": list(origin), "end": list(end),
                "radius": radius, "cell_size_m": self.index.cell_size_m,
                "tiers": [ACTIVE, PROJECTILE]})["hits"]
            self.index.stats.queries += 1
            self.index.stats.candidates_considered += len(found)

            hits: List[HitResult] = []
            for entity_id, distance_m in found:
                entry = self.index.entry(entity_id)
                cand = _candidate_from(entry, distance_m)
                skeleton = _require_rig(self.skeletons, cand,
                                        self.index.cell_id)

                if entry.tier != ACTIVE:
                    self.stats.body_tests += 1
                    body = skeleton.whole_body_capsule()
                    if not ray_capsule_hit(origin, direction, body,
                                           max_distance):
                        continue

                packed = boxes_of.get(entity_id)
                if packed is None:
                    limb_state = view.limb_state(entity_id,
                                                 purpose=HIT_TEST_ONLY)
                    # Cached in the shape the next kernel wants, and now
                    # *built* in it: `hitbox_rows` reads the pose kernel's
                    # bytes straight into these lists. It used to call
                    # `hitboxes()` and take the `Capsule`s apart again, so six
                    # capsules were constructed per rig per volley purely to be
                    # discarded (D54).
                    packed = skeleton.hitbox_rows(limb_state=limb_state,
                                                  place=place)
                    boxes_of[entity_id] = packed

                self.stats.bone_refinements += 1
                self.stats.capsule_tests += len(packed)
                answer = nearest({
                    "boxes": packed,
                    "origin": list(origin), "direction": list(direction),
                    "max_distance": max_distance})
                region, precise = answer["region"], answer["precise"]

                if entry.tier == ACTIVE and not precise:
                    continue
                if region is None:
                    continue
                hits.append(self._report(cand, region, force, source_id,
                                         region_precise=precise,
                                         pose_version=skeleton.pose_version))
                if first_hit_only:
                    break
            out.append(hits)

        self._charge()
        return out

    def _volley_entries(self) -> List[List[Any]]:
        """The spatial index as a kernel payload. Built once per volley."""
        return [[e.entity_id, list(e.position), e.tier]
                for e in self.index.entries()]

    def _region_for_ray(self, view: Any, entity_id: str, skeleton: Skeleton,
                        origin: Vec3, direction: Vec3, max_distance: float
                        ) -> Tuple[Optional[str], bool]:
        """Which region a ray struck, and whether a bone capsule was actually
        intersected.

        The live limb-state read happens here, for every tier. Scope 5 puts no
        tier condition on it - "a severed limb offers no hitbox" is true of a
        sniper's target two cells away exactly as it is of the man in front of
        you.

        When no bone is intersected, the *nearest* bone is returned with
        `precise=False`. That is a measurement, not a guess: every capsule was
        tested and the minimum taken. The caller decides what a non-precise
        region means - ACTIVE treats it as a miss, PROJECTILE treats it as a
        graze, because for PROJECTILE the body capsule already established that
        something was struck.
        """
        limb_state = view.limb_state(entity_id, purpose=HIT_TEST_ONLY)
        boxes = skeleton.hitboxes(limb_state=limb_state)
        self.stats.bone_refinements += 1
        self.stats.capsule_tests += len(boxes)
        return nearest_region(boxes, origin, direction, max_distance)

    # -- blasts / mass casualty ---------------------------------------------
    def resolve_blast(self, view: Any, center: Vec3, radius: float,
                      force: float, *, zone_id: Optional[str] = None,
                      source_id: Optional[str] = None) -> List[HitResult]:
        """Everything in a blast radius, at whatever fidelity its tier allows.

        Scope 7: "resolving 'which of 40 villagers in a blast radius got hit' is
        a zone-occupancy query + `apply_effect` per occupant [...] Lobster's job
        is presentational". So this reports; it does not apply anything. When a
        `zone_id` is given, the occupancy query decides who is present and the
        spatial index only decides how precisely each one can be tested -
        occupancy is Octopus's answer, and Lobster does not second-guess it.
        """
        self.stats.blasts += 1
        present: Optional[set] = None
        if zone_id is not None:
            present = {occ.get("character_id") or occ.get("id")
                       for occ in view.zone_occupants(zone_id)}
        out: List[HitResult] = []
        for cand in self.index.query_sphere(center, radius):
            if cand.tier == DORMANT:
                continue  # no hitbox; 6.5's Event path covers them
            if present is not None and cand.entity_id not in present:
                continue
            skeleton = _require_rig(self.skeletons, cand, self.index.cell_id)
            region: Optional[str] = None
            pose_version = 0
            if skeleton is not None:
                # Every tier with a hitbox gets a region, for the same reason
                # projectiles do (D16). There is no ray here, so the region is
                # the bone nearest the epicentre and `region_precise` is False.
                limb_state = view.limb_state(cand.entity_id,
                                             purpose=HIT_TEST_ONLY)
                boxes = skeleton.hitboxes(limb_state=limb_state)
                self.stats.bone_refinements += 1
                self.stats.capsule_tests += len(boxes)
                pose_version = skeleton.pose_version
                best = None
                for candidate_region, capsule in boxes:
                    centre = tuple((capsule.a[i] + capsule.b[i]) * 0.5
                                   for i in range(3))
                    d = distance(center, centre)
                    if best is None or d < best:
                        best, region = d, candidate_region
            out.append(self._report(cand, region, force, source_id,
                                    pose_version=pose_version))
        self._charge()
        return out

    # -- reporting -----------------------------------------------------------
    def _report(self, cand: Candidate, region: Optional[str], force: float,
                source_id: Optional[str], *, region_precise: bool = False,
                pose_version: int = 0) -> HitResult:
        result = HitResult(target_id=cand.entity_id, region=region,
                           force=float(force), source_id=source_id,
                           tier=cand.tier, snapshot_seq=cand.snapshot_seq,
                           snapshot_reason=cand.snapshot_reason,
                           region_precise=region_precise,
                           pose_version=pose_version,
                           cell_id=self.index.cell_id)
        if self.bus is not None:
            self.bus.hit_location(result.target_id, result.region,
                                  result.force, result.source_id)
        return result

    def fidelity_for(self, entity_id: str) -> str:
        return FIDELITY[self.index.tier_of(entity_id)]


# ---------------------------------------------------------------------------
# Across the cell boundary
# ---------------------------------------------------------------------------

class WorldHitTester:
    """One shot, resolved against every resident cell (Shrimp finding #8).

    CONTRACT §3 defines PROJECTILE as "within the range of a projectile
    currently being resolved, **in whatever cell**", and Scope §7 says
    "regardless of connection-graph adjacency". CONTRACT §1 promises "a sniper's
    shot two cells away resolves to a limb exactly as a sword blow does". None of
    that was true: `HitTester` is built around one `ResidentCell.index`, so a
    shot stopped at the cell boundary. Placement (D21) turned that from a
    theoretical gap into a visible one - a bandit 16 m away across the boundary
    is on screen, inside a 45 m shot, and was unhittable.

    **How it works: the ray moves, the world does not.** Each cell keeps its own
    local coordinates, its own spatial index and its own snapshot provenance; the
    ray is transformed into each cell's space by the inverse of that cell's
    placement, and the ordinary per-cell path runs unchanged. Merging every cell
    into one world-space index would have been the other option, and it would
    have meant rebuilding an index per frame and throwing away the entry/exit
    snapshot rule that Scope §7 spends a paragraph on.

    **Deliberately projectiles only.** Melee stays single-cell because ACTIVE is
    defined as "current cell + immediate melee range" - a sword does not reach
    the next cell, so a cross-cell swing is not an omission. Blasts already have
    a cross-cell path: Scope §6.5 resolves mass-casualty through zone occupancy,
    which is Octopus records and cell-agnostic by construction.

    **No tier changes.** PROJECTILE is still never passed to Octopus, and being
    shot from another cell promotes nothing.

    `resolve_volley` batches a whole volley across the resident set, per cell
    rather than per shot (D55). `resolve_projectile` is the same thing for one
    shot and is what the volley is asserted against.
    """

    def __init__(self, cells: Iterable[Any],
                 placements: Optional[Mapping[str, Any]] = None,
                 bus: Optional[EventBus] = None, **ceilings: Any) -> None:
        from .geometry import Transform
        self.cells = {cell.cell_id: cell for cell in cells}
        self.placements = dict(placements or {})
        self.bus = bus
        self._identity = Transform()
        # The sub-testers get NO bus. They each resolve their own cell and the
        # nearest wins, so letting them report would fire on_hit_location for
        # every candidate this shot passed through on its way to the one it
        # actually hit - a bandit taking damage from a bullet that stopped in
        # the guard in front of him. Reporting belongs where selection happens.
        self.testers: Dict[str, HitTester] = {
            cell_id: HitTester(cell.index, cell.skeletons, None, **ceilings)
            for cell_id, cell in self.cells.items()}

    @classmethod
    def from_manager(cls, manager: Any, view: Any,
                     bus: Optional[EventBus] = None,
                     **ceilings: Any) -> "WorldHitTester":
        """The common case: everything resident, placed as its records say."""
        return cls([manager.resident[cell_id]
                    for cell_id in sorted(manager.resident)],
                   manager.placements(view), bus, **ceilings)

    # -- frame ---------------------------------------------------------------
    def begin_frame(self) -> None:
        for tester in self.testers.values():
            tester.begin_frame()

    def stats(self) -> HitTestStats:
        """Combined cost across every cell this shot touched."""
        total = HitTestStats()
        for tester in self.testers.values():
            for name in total.__dataclass_fields__:
                setattr(total, name,
                        getattr(total, name) + getattr(tester.stats, name))
        return total

    def placement_of(self, cell_id: str) -> Any:
        return self.placements.get(cell_id, self._identity)

    # -- the shot ------------------------------------------------------------
    def resolve_projectile(self, view: Any, origin: Vec3, direction: Vec3,
                           max_distance: float, force: float, *,
                           source_id: Optional[str] = None,
                           first_hit_only: bool = True) -> List[HitResult]:
        """A shot in **world space**, resolved against every resident cell.

        Returns hits ordered by distance from the shooter, nearest first. With
        `first_hit_only` the single nearest hit is returned - which has to be
        decided after every cell has answered, not by taking whichever cell was
        asked first, or a shot would hit the far bandit through the near one.
        """
        heading = normalize(direction)
        if heading == (0.0, 0.0, 0.0):
            raise HitTestError("a projectile needs a direction")

        found: List[HitResult] = []
        for cell_id in sorted(self.cells):
            placement = self.placement_of(cell_id)
            if not self._ray_reaches(cell_id, placement, origin, heading,
                                     max_distance):
                continue
            local_origin = placement.inverse_apply(origin)
            local_heading = placement.inverse_rotate(heading)
            hits = self.testers[cell_id].resolve_projectile(
                view, local_origin, local_heading, max_distance, force,
                source_id=source_id, first_hit_only=first_hit_only)
            for hit in hits:
                found.append(self._with_range(hit, cell_id, placement, origin))

        found.sort(key=lambda h: (h.distance_from_source, h.target_id))
        if first_hit_only and found:
            found = found[:1]
        if self.bus is not None:
            for hit in found:
                self.bus.hit_location(hit.target_id, hit.region, hit.force,
                                      hit.source_id)
        return found

    def resolve_volley(self, view: Any, shots: Sequence[Any], *,
                       first_hit_only: bool = True) -> List[List[HitResult]]:
        """A whole volley in **world space**, across every resident cell.

        `shots` are `(origin, direction, max_distance, force, source_id)`
        tuples in world space; the result is one list of hits per shot, in the
        same order.

        > **Equivalent to N `resolve_projectile`s**, and a test asserts that
        > directly - same hits, same order, same Events. A faster path that
        > answers differently is not a faster path (D45).

        **The batching is per cell, not per shot.** `HitTester.resolve_volley`
        shares three things across a volley - the packed entity table, each
        rig's resolved hitboxes, and one budget charge - and resolving a
        cross-cell volley shot by shot would rebuild all three once per arrow
        per cell. So each cell is handed the shots that reach *it*, once.

        Two things this must not get wrong, both of which `resolve_projectile`
        already gets right one shot at a time:

        * **The merge is by world distance**, decided after every cell has
          answered. Each cell answers in its own coordinates, so taking
          whichever cell was asked first would let a shot hit the far bandit
          through the near one.
        * **A shot that cannot reach a cell is not sent to it.** The same
          `_ray_reaches` gate, applied per shot rather than per call - so a
          volley fanned across a wide arc does not pay a sweep in every cell
          for every arrow that was never pointed at it.
        """
        shots = list(shots)
        if not shots:
            return []

        headings: List[Vec3] = []
        for _origin, direction, _max_distance, _force, _source in shots:
            heading = normalize(direction)
            if heading == (0.0, 0.0, 0.0):
                raise HitTestError("a projectile needs a direction")
            headings.append(heading)

        found: List[List[HitResult]] = [[] for _ in shots]
        for cell_id in sorted(self.cells):
            placement = self.placement_of(cell_id)
            local: List[Any] = []
            slots: List[int] = []
            for index, shot in enumerate(shots):
                origin, _direction, max_distance, force, source_id = shot
                heading = headings[index]
                if not self._ray_reaches(cell_id, placement, origin, heading,
                                         max_distance):
                    continue
                slots.append(index)
                local.append((placement.inverse_apply(origin),
                              placement.inverse_rotate(heading),
                              max_distance, force, source_id))
            if not local:
                continue
            answers = self.testers[cell_id].resolve_volley(
                view, local, first_hit_only=first_hit_only)
            for slot, hits in zip(slots, answers):
                origin = shots[slot][0]
                for hit in hits:
                    found[slot].append(
                        self._with_range(hit, cell_id, placement, origin))

        out: List[List[HitResult]] = []
        for hits in found:
            hits.sort(key=lambda h: (h.distance_from_source, h.target_id))
            out.append(hits[:1] if (first_hit_only and hits) else hits)

        # Fired in shot order, after every merge, so the sequence a subscriber
        # sees is the one N separate calls would have produced.
        if self.bus is not None:
            for hits in out:
                for hit in hits:
                    self.bus.hit_location(hit.target_id, hit.region, hit.force,
                                          hit.source_id)
        return out

    def _with_range(self, hit: HitResult, cell_id: str, placement: Any,
                    origin: Vec3) -> HitResult:
        """Stamp world-space range onto a hit resolved in cell-local space."""
        index = self.cells[cell_id].index
        try:
            local = index.entry(hit.target_id).position
        except Exception:                   # pragma: no cover - defensive
            return hit
        world = placement.apply(local)
        return HitResult(
            target_id=hit.target_id, region=hit.region, force=hit.force,
            source_id=hit.source_id, tier=hit.tier,
            snapshot_seq=hit.snapshot_seq, snapshot_reason=hit.snapshot_reason,
            region_precise=hit.region_precise, pose_version=hit.pose_version,
            cell_id=cell_id, distance_from_source=distance(origin, world))

    def _ray_reaches(self, cell_id: str, placement: Any, origin: Vec3,
                     heading: Vec3, max_distance: float) -> bool:
        """Could this shot possibly touch this cell at all?

        A cheap rejection so a shot does not pay a broad-phase sweep in every
        resident cell just to find nothing. Bounds come from the cell's terrain
        where it has any, and the test is segment-versus-box: a bounding sphere
        is useless here, because a 128 m cell that is barely any height gets a
        90 m radius and a shot fired *away* from it still "reaches" it.

        The box is inflated by the broad margin, so it errs outward like every
        other gate - a cell wrongly included costs one sweep that finds nothing,
        a cell wrongly excluded loses a hit silently.
        """
        from .geometry import AABB, segment_aabb_overlap
        from .constants import EXTERIOR_CELL_SIZE_M
        cell = self.cells[cell_id]
        terrain = getattr(cell, "terrain", None)
        bounds = terrain.mesh.world_bounds() if terrain is not None else None
        if bounds is None:
            side = EXTERIOR_CELL_SIZE_M
            bounds = AABB((0.0, -side, 0.0), (side, side, side))
        lo, hi = bounds.minimum, bounds.maximum
        corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                   for z in (lo[2], hi[2])]
        world_box = AABB.from_points(placement.apply(c) for c in corners)
        margin = self.testers[cell_id].broad_margin()
        end = tuple(origin[i] + heading[i] * max_distance for i in range(3))
        return segment_aabb_overlap(origin, end, world_box.expanded(margin))
