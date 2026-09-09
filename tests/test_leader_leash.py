"""Section 14 test 6 - leader-leash queuing (v0.3).

> a war party through a single-file bottleneck never gets a follower stuck; the
> queue forms with **zero formation-specific logic**.

Both halves matter, and the second is the one that decays. It is easy to make a
party get through a doorway by writing bottleneck-detection code; the Scope
explicitly does not want that, because the collapse behaviour

> isn't special-case logic - it's the natural output of a simpler rule

so this file asserts the outcome *and* asserts that the outcome was not
engineered: the same code path with four different offset patterns behaves the
same way, and the movement primitive never looks at which pattern is in use.
"""

from __future__ import annotations

import inspect
import unittest

from lobster import movement as movement_module
from lobster.movement import (Agent, CIRCLE, HELD_BLOCKED_BY_AGENT, LINE,
                              LeaderLeash, SCATTER, WEDGE, formation_offset)
from tests.fixtures import VILLAGE, bottleneck_navmesh

CORRIDOR_X = 0.6          # the corridor's centre line
DESTINATION = (CORRIDOR_X, 0.0, 15.0)


def make_party(size: int = 5, *, pattern: str = LINE):
    """A leader and `size - 1` followers, bunched at the corridor mouth.

    Everyone starts *on* the navmesh: an agent standing off it has no path by
    definition, and a test that began there would be measuring the fixture
    rather than the leash.
    """
    agents = {}
    ids = ["leader"] + ["follower-{0}".format(i) for i in range(size - 1)]
    for i, entity_id in enumerate(ids):
        z = 0.9 * (size - i)
        agents[entity_id] = Agent(entity_id, (CORRIDOR_X, 0.0, z),
                                  speed=3.0, radius=0.4)
    leash = LeaderLeash("leader", ids[1:], pattern=pattern)
    return agents, leash


def run(agents, leash, navmesh, *, frames: int = 400, dt: float = 0.05):
    """Run the party for a while and report what happened."""
    holds = {aid: [] for aid in agents}
    for _ in range(frames):
        for result in leash.step(navmesh, agents, DESTINATION, dt):
            if not result.moved and result.reason:
                holds[result.entity_id].append(result.reason)
    return holds


class TestLeaderLeashQueuing(unittest.TestCase):

    def setUp(self):
        self.navmesh = bottleneck_navmesh(VILLAGE)

    def assert_party_not_stuck(self, agents, leash, starts, holds):
        """"No follower ever gets stuck", stated in terms a queue satisfies.

        A queue *is* followers not moving for part of the run - that is the
        point - so "stuck" cannot mean "held". It means: made no progress, or
        ended up with nowhere to go. A follower that is waiting behind the one
        ahead of it is working exactly as designed.
        """
        leader = agents["leader"]
        self.assertGreater(leader.position[2], 12.0,
                           "the leader should have crossed the corridor")
        order = ["leader"] + leash.follower_ids
        for entity_id in leash.follower_ids:
            travelled = agents[entity_id].position[2] - starts[entity_id]
            self.assertGreater(travelled, 5.0,
                               "{0} made no progress down the corridor".format(
                                   entity_id))
            tail = holds[entity_id][-5:]
            self.assertNotIn(
                "no_walkable_path", tail,
                "{0} ended the run with no route at all - that is stuck, not "
                "queued".format(entity_id))
        # the party is contiguous: nobody has been left behind by the queue
        for ahead, behind in zip(order, order[1:]):
            gap = agents[ahead].position[2] - agents[behind].position[2]
            self.assertLess(
                gap, 2.5,
                "{0} is {1:.2f} m ahead of {2} - the leash has stretched into a "
                "gap rather than a queue".format(ahead, gap, behind))

    def test_no_follower_is_ever_stuck(self):
        agents, leash = make_party(5)
        starts = {aid: a.position[2] for aid, a in agents.items()}
        holds = run(agents, leash, self.navmesh)
        self.assert_party_not_stuck(agents, leash, starts, holds)

    def test_the_queue_forms_by_itself(self):
        """Followers end up single file, in order, without anything ordering
        them."""
        agents, leash = make_party(5)
        holds = run(agents, leash, self.navmesh, frames=120)

        blocked = sum(1 for reasons in holds.values()
                      for reason in reasons if reason == HELD_BLOCKED_BY_AGENT)
        self.assertGreater(blocked, 0,
                           "nobody ever waited for anybody - this corridor is "
                           "not actually a bottleneck, so the test proves "
                           "nothing")

        zs = [agents[aid].position[2]
              for aid in ["leader"] + leash.follower_ids]
        for ahead, behind in zip(zs, zs[1:]):
            self.assertGreaterEqual(
                ahead, behind - 0.05,
                "the party is not in single file: {0}".format(zs))

    def test_every_formation_pattern_behaves_the_same(self):
        """"the leash mechanism underneath is identical regardless of which
        pattern is chosen"."""
        for pattern in (LINE, WEDGE, CIRCLE, SCATTER):
            with self.subTest(pattern=pattern):
                agents, leash = make_party(5, pattern=pattern)
                starts = {aid: a.position[2] for aid, a in agents.items()}
                holds = run(agents, leash, self.navmesh)
                self.assert_party_not_stuck(agents, leash, starts, holds)

    def test_an_unreachable_offset_falls_back_to_the_leader(self):
        """Scope 15.9 - the offset is a leash target, never a hard constraint."""
        agents, leash = make_party(3)
        leader = agents["leader"]
        leader.position = (CORRIDOR_X, 0.0, 5.0)
        # a LINE offset puts follower 0 outside a 1.2 m corridor
        offset = formation_offset(LINE, 0, 2)
        self.assertNotEqual(offset[0], 0.0)
        target = leash._leash_target(self.navmesh, leader, 0, 2)
        self.assertEqual(target, leader.position,
                         "an offset that lands off the navmesh must resolve to "
                         "the leader, not to an impossible position")

    def test_a_wide_space_does_honour_the_offset(self):
        """The fallback is a fallback, not the only behaviour."""
        from tests.fixtures import corridor_navmesh
        wide = corridor_navmesh(VILLAGE, width=8.0)
        agents, leash = make_party(3)
        leader = agents["leader"]
        leader.position = (4.0, 0.0, 6.0)
        target = leash._leash_target(wide, leader, 0, 2)
        self.assertNotEqual(target, leader.position)


class TestNoFormationSpecificLogic(unittest.TestCase):
    """The "zero formation-specific logic" half of the test."""

    def test_the_movement_primitive_never_reads_the_pattern(self):
        source = inspect.getsource(movement_module._walk_towards)
        for name in ("pattern", "LINE", "WEDGE", "CIRCLE", "SCATTER",
                     "formation"):
            self.assertNotIn(name, source,
                             "_walk_towards branches on {0!r} - the leash step "
                             "must be identical for every formation".format(name))

    def test_there_is_no_bottleneck_detection(self):
        source = inspect.getsource(movement_module).lower()
        code = " ".join(line for line in source.splitlines()
                        if not line.strip().startswith(("#", ">", "*")))
        for banned in ("detect_bottleneck", "single_file_mode", "reassign_slot",
                       "queue_index", "fall_in_behind"):
            self.assertNotIn(banned, code,
                             "{0!r} is exactly the special-case logic Scope 9 "
                             "exists to avoid".format(banned))

    def test_offsets_are_a_pure_function(self):
        for pattern in (LINE, WEDGE, CIRCLE, SCATTER):
            first = formation_offset(pattern, 2, 5)
            second = formation_offset(pattern, 2, 5)
            self.assertEqual(first, second,
                             "formation offsets must be deterministic - two "
                             "runs of the same party produce the same "
                             "positions")

    def test_there_is_no_tactical_ai(self):
        """L8 / Scope 9: engagement and target selection stay out of Lobster."""
        for name in ("select_target", "engage", "flank", "hold_position",
                     "acquire_target", "morale"):
            self.assertFalse(hasattr(movement_module, name),
                             "lobster.movement exposes {0} - tactical intent is "
                             "AIPackage content, not the geometry layer".format(
                                 name))


if __name__ == "__main__":
    unittest.main()
