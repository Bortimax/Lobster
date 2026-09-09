"""Leader-leash movement primitives (Scope 9, L8).

> - The **leader** pathfinds to the actual destination.
> - **Followers do not pathfind to the destination.** Each follower pathfinds
>   toward the leader's current position plus a small formation offset.
> - If a follower can't reach its offset (blocked by terrain, a doorway,
>   another entity), it **waits and retries next frame** rather than forcing the
>   offset.
> - This naturally produces a queue [...] with zero formation-specific
>   bottleneck-detection code.

The queue is not implemented. That is the point of the design, and the section
14 test is written to fail if anyone ever implements it: there is no bottleneck
detection here, no reservation system, no slot reassignment, no "if blocked,
fall in behind". A follower moves toward its leash target, refuses to walk
through another agent, and tries again next frame. Everything else is emergent.

Scope 15.9 names the regression to watch for:

> Formation offsets that get treated as hard constraints, instead of leash
> targets, will get followers stuck in doorways. If a future change
> reintroduces "pathfind to formation position" instead of "pathfind toward the
> leader," that's a regression back to the bug 9 exists to avoid.

So `_leash_target` falls back to the leader's own position whenever the offset
point is not reachable. The offset is a preference; the leader is the target.

What is deliberately absent (L8, Scope 9): engagement, target selection, holding
ground, flanking, morale, and any notion of what the party is *for*. A war party
here is a leader id, some follower ids, and an offset pattern.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Tuple)

from .geometry import Vec3, add, distance, normalize, scale, sub
from .navmesh import Navmesh

# ---------------------------------------------------------------------------
# Formation patterns - data, not behaviour
# ---------------------------------------------------------------------------

LINE = "line"
WEDGE = "wedge"
CIRCLE = "circle"
SCATTER = "scatter"

FORMATIONS: Tuple[str, ...] = (LINE, WEDGE, CIRCLE, SCATTER)


class MovementError(Exception):
    pass


def formation_offset(pattern: str, slot: int, count: int, *,
                     spacing: float = 1.5) -> Vec3:
    """Where follower `slot` would stand if nothing were in the way.

    "**Formation types** (`line`, `wedge`, `circle`, `scatter`) are just the
    offset pattern a war party's `AIPackage` selects; the leash mechanism
    underneath is identical regardless of which pattern is chosen." So this is a
    pure function of (pattern, slot, count) and the leash step below never
    branches on the pattern at all.

    Offsets are in the leader's local frame, -Z being behind them.
    """
    if pattern not in FORMATIONS:
        raise MovementError(
            "unknown formation {0!r}; the set is {1}. A new pattern is content, "
            "not a code path - add it here as an offset function, never as a "
            "branch in the leash step.".format(pattern, list(FORMATIONS)))
    if pattern == LINE:
        lateral = (slot - (count - 1) / 2.0) * spacing
        return (lateral, 0.0, -spacing)
    if pattern == WEDGE:
        side = 1 if slot % 2 else -1
        rank = slot // 2 + 1
        return (side * rank * spacing * 0.8, 0.0, -rank * spacing)
    if pattern == CIRCLE:
        angle = 2.0 * math.pi * slot / max(1, count)
        radius = spacing * 1.5
        return (math.sin(angle) * radius, 0.0, math.cos(angle) * radius)
    # SCATTER: deterministic pseudo-spread, no RNG - two runs of the same
    # party must produce the same positions (Octopus's determinism rule).
    golden = 2.399963
    angle = slot * golden
    radius = spacing * (1.0 + 0.5 * (slot % 3))
    return (math.sin(angle) * radius, 0.0, -abs(math.cos(angle)) * radius)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    """One moving body. Geometry only - no health, no intent, no state machine."""

    entity_id: str
    position: Vec3
    speed: float = 3.0
    radius: float = 0.4
    facing: Vec3 = (0.0, 0.0, 1.0)

    def to_dict(self) -> Dict[str, Any]:
        return {"entity_id": self.entity_id, "position": list(self.position),
                "speed": self.speed, "radius": self.radius}


#: Why an agent did not move this step. Reported, never acted on.
HELD_NO_PATH = "no_walkable_path"
HELD_NAVMESH_PENDING = "navmesh_pending"
HELD_BLOCKED_BY_AGENT = "blocked_by_agent"
HELD_ARRIVED = "arrived"


@dataclass(frozen=True)
class StepResult:
    entity_id: str
    moved: bool
    position: Vec3
    reason: Optional[str] = None
    target: Optional[Vec3] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"entity_id": self.entity_id, "moved": self.moved,
                "position": list(self.position), "reason": self.reason,
                "target": list(self.target) if self.target else None}


class LeaderLeash:
    """A war party: one leader, some followers, one offset pattern.

    `step` advances everybody by one frame. It returns what happened, which is
    all Lobster has to say about it.
    """

    def __init__(self, leader_id: str, follower_ids: Sequence[str], *,
                 pattern: str = LINE, spacing: float = 1.5,
                 leash_tolerance: float = 0.6) -> None:
        if pattern not in FORMATIONS:
            raise MovementError("unknown formation {0!r}".format(pattern))
        self.leader_id = leader_id
        self.follower_ids = list(follower_ids)
        self.pattern = pattern
        self.spacing = spacing
        self.leash_tolerance = leash_tolerance

    # -- leader --------------------------------------------------------------
    def step_leader(self, navmesh: Navmesh, agent: Agent, destination: Vec3,
                    dt: float) -> StepResult:
        """The leader is the only one that pathfinds to the destination."""
        return _walk_towards(navmesh, agent, destination, dt, others=())

    # -- followers -----------------------------------------------------------
    def step_followers(self, navmesh: Navmesh, agents: Dict[str, Agent],
                       dt: float) -> List[StepResult]:
        leader = agents.get(self.leader_id)
        if leader is None:
            raise MovementError(
                "leader {0!r} has no agent; a leash with no leader is not a "
                "formation, it is a bug".format(self.leader_id))
        out: List[StepResult] = []
        count = len(self.follower_ids)
        for slot, follower_id in enumerate(self.follower_ids):
            follower = agents.get(follower_id)
            if follower is None:
                continue
            target = self._leash_target(navmesh, leader, slot, count)
            others = [a for aid, a in agents.items() if aid != follower_id]
            out.append(_walk_towards(navmesh, follower, target, dt, others=others,
                                     arrive_within=self.leash_tolerance))
        return out

    def _leash_target(self, navmesh: Navmesh, leader: Agent, slot: int,
                      count: int) -> Vec3:
        """Offset if it is reachable, the leader otherwise.

        This one fallback is the whole anti-regression guard from 15.9: the
        offset never becomes a hard constraint, so a follower can never be stuck
        holding a geometrically impossible position.
        """
        offset = formation_offset(self.pattern, slot, count, spacing=self.spacing)
        rotated = _rotate_to_facing(offset, leader.facing)
        point = add(leader.position, rotated)
        poly = navmesh.poly_at(point)
        if poly is not None and navmesh.is_walkable(poly) and not navmesh.is_dirty(poly):
            return point
        return leader.position

    def step(self, navmesh: Navmesh, agents: Dict[str, Agent],
             destination: Vec3, dt: float) -> List[StepResult]:
        leader = agents.get(self.leader_id)
        if leader is None:
            raise MovementError("leader {0!r} has no agent".format(self.leader_id))
        results = [self.step_leader(navmesh, leader, destination, dt)]
        results.extend(self.step_followers(navmesh, agents, dt))
        return results


# ---------------------------------------------------------------------------
# The one movement primitive
# ---------------------------------------------------------------------------

def _rotate_to_facing(offset: Vec3, facing: Vec3) -> Vec3:
    f = normalize((facing[0], 0.0, facing[2]))
    if f == (0.0, 0.0, 0.0):
        f = (0.0, 0.0, 1.0)
    right = (f[2], 0.0, -f[0])
    return (right[0] * offset[0] + f[0] * offset[2], offset[1],
            right[2] * offset[0] + f[2] * offset[2])


def _walk_towards(navmesh: Navmesh, agent: Agent, target: Vec3, dt: float, *,
                  others: Iterable[Agent] = (),
                  arrive_within: float = 0.25) -> StepResult:
    """One frame of movement toward a point, or an explained refusal to move.

    The refusals, in order, and each one a *hold* rather than a workaround:

    * the target is already reached - nothing to do;
    * no walkable path exists, or the route crosses a polygon whose navmesh
      patch is still pending (Scope 10) - hold, do not commit to a route that
      may be invalidated next frame;
    * the next step would put this agent inside another - hold and retry next
      frame, which is the line that makes a queue form by itself.
    """
    if distance(agent.position, target) <= arrive_within:
        return StepResult(agent.entity_id, False, agent.position, HELD_ARRIVED,
                          target)

    start = navmesh.poly_at(agent.position)
    goal = navmesh.poly_at(target)
    if start is None or goal is None:
        return StepResult(agent.entity_id, False, agent.position, HELD_NO_PATH,
                          target)
    if navmesh.is_dirty(start) or navmesh.is_dirty(goal):
        return StepResult(agent.entity_id, False, agent.position,
                          HELD_NAVMESH_PENDING, target)
    path = navmesh.find_path(start, goal, avoid_dirty=True)
    if path is None:
        reason = (HELD_NAVMESH_PENDING
                  if navmesh.find_path(start, goal, avoid_dirty=False) is not None
                  else HELD_NO_PATH)
        return StepResult(agent.entity_id, False, agent.position, reason, target)

    waypoint = target if len(path) <= 1 else navmesh.poly(path[1]).center()
    direction = sub(waypoint, agent.position)
    if distance((0.0, 0.0, 0.0), direction) < 1e-9:
        waypoint = target
        direction = sub(waypoint, agent.position)
    step = min(agent.speed * dt, distance(agent.position, waypoint))
    candidate = add(agent.position, scale(normalize(direction), step))

    for other in others:
        if distance(candidate, other.position) < agent.radius + other.radius:
            return StepResult(agent.entity_id, False, agent.position,
                              HELD_BLOCKED_BY_AGENT, target)

    agent.position = candidate
    heading = normalize(direction)
    if heading != (0.0, 0.0, 0.0):
        agent.facing = heading
    return StepResult(agent.entity_id, True, agent.position, None, target)
