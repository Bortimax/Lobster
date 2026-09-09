"""Section 14 test 3 - the split limb-state round trip (v0.3).

> **Split limb-state round trip** - Lobster's hit-test correctly loses the
> hitbox on sever; Shrimp's animation controller, independently, correctly stops
> posing that limb. **Two assertions, not one.**

The emphasis is the test. It would be easy to write one assertion, have Lobster
tell the animation controller what happened, and call it green - and that would
be exactly the L8 violation Scope 15.4 warns about:

> the fix for the old bug (stale hitboxes) shouldn't grow into scope creep
> (Lobster becoming an animation controller).

So the two halves here share a *field*, and nothing else: no call, no callback,
no shared object. The last two tests in this file assert that separation
directly, because the round trip alone cannot see it.
"""

from __future__ import annotations

import inspect
import unittest

from lobster import hittest as hittest_module
from lobster import skeleton as skeleton_module
from lobster.events import EventBus
from lobster.geometry import Capsule, Transform
from lobster.hittest import HitTester
from lobster.octopus_bridge import (ContractError, HIT_TEST_ONLY,
                                    OctopusBridge, limb_has_hitbox)
from lobster.skeleton import LEFT_ARM, Skeleton, humanoid_region_set
from lobster.spatial import SpatialIndex
from lobster.tiers import ACTIVE
from tests.fixtures import VILLAGE, build_session
from tests.shrimp_mock import MockAnimationController

ADA = "npc-ada"


def sever(session, entity_id: str, limb_id: str) -> None:
    """What Octopus's stats module does on a threshold crossing (Scope 5)."""
    session.engine.write({"op": "PATCH", "id": entity_id,
                          "field": "limb_state." + limb_id,
                          "value": "severed"})


class TestSplitLimbStateRoundTrip(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.skeleton = Skeleton(ADA, humanoid_region_set(),
                                 root=Transform(position=(10.0, 0.0, 10.0)))
        self.index = SpatialIndex(VILLAGE)
        self.index.add(ADA, (10.0, 0.0, 10.0), ACTIVE)
        self.tester = HitTester(self.index, {ADA: self.skeleton}, EventBus())
        self.controller = MockAnimationController(self.session, self.skeleton)
        # a swing volume that sweeps the whole body, so region selection is
        # about which hitboxes exist rather than about where the sword went
        self.swing = Capsule((8.5, 1.0, 10.0), (11.5, 1.0, 10.0), 1.2)

    def regions_hit(self):
        with self.bridge.frame() as view:
            results = self.tester.resolve_swing(
                view, self.swing, force=10.0, source_id="player",
                max_regions_per_target=6)
        return {r.region for r in results}

    def test_intact_arm_has_a_hitbox_and_is_posed(self):
        self.assertIn(LEFT_ARM, self.regions_hit())
        self.assertIn("arm_l", self.controller.tick(ADA))

    def test_severed_limb_loses_hitbox_and_stops_being_posed(self):
        sever(self.session, ADA, "left_arm")

        # assertion one: Lobster's next hit-test finds no hitbox on the stump
        self.assertNotIn(LEFT_ARM, self.regions_hit(),
                         "a severed limb must offer no hitbox")

        # assertion two: Shrimp's controller, independently, stops posing it
        self.assertNotIn("arm_l", self.controller.tick(ADA),
                         "the animation controller must reach the same "
                         "conclusion on its own")

        # ... and the rest of the body is unaffected in both systems
        self.assertIn("right_arm", self.regions_hit())
        self.assertIn("arm_r", self.controller.posed_bones)

    def test_lobster_did_not_tell_shrimp(self):
        """The two halves share a field, not a call."""
        before = self.controller.reads
        sever(self.session, ADA, "left_arm")
        self.regions_hit()
        self.assertEqual(self.controller.reads, before,
                         "the hit-test must not have driven the animation "
                         "controller - that is Lobster becoming Shrimp (L8)")
        self.controller.tick(ADA)
        self.assertGreater(self.controller.reads, before)

    def test_the_read_is_live_not_cached(self):
        """A sever between two hit-tests changes the second one."""
        self.assertIn(LEFT_ARM, self.regions_hit())
        sever(self.session, ADA, "left_arm")
        self.assertNotIn(LEFT_ARM, self.regions_hit())

    def test_a_disabled_limb_still_has_a_hitbox(self):
        """Only `severed` removes geometry.

        What a disabled arm *means* is Octopus's stats module and Shrimp's
        content (Scope 5); Lobster is not entitled to an opinion, and removing
        the hitbox would be one.
        """
        self.session.engine.write({"op": "PATCH", "id": ADA,
                                   "field": "limb_state.left_arm",
                                   "value": "disabled"})
        self.assertIn(LEFT_ARM, self.regions_hit())
        self.assertTrue(limb_has_hitbox("disabled"))
        self.assertFalse(limb_has_hitbox("severed"))
        self.assertTrue(limb_has_hitbox(None))


class TestLimbStateBoundary(unittest.TestCase):
    """L8, enforced rather than described."""

    def test_reading_limb_state_requires_the_hit_test_sentinel(self):
        session = build_session()
        view = OctopusBridge(session).frame()
        with self.assertRaises(ContractError) as ctx:
            view.limb_state(ADA, purpose="animation")
        self.assertIn("hit-test", str(ctx.exception))
        self.assertIn("Shrimp", str(ctx.exception))

    def test_the_skeleton_module_cannot_read_limb_state(self):
        """`Skeleton.hitboxes` takes the map as an argument, never fetches it."""
        source = inspect.getsource(skeleton_module)
        code = " ".join(line for line in source.splitlines()
                        if not line.lstrip().startswith(("#", ">", "*")))
        self.assertNotIn("purpose=HIT_TEST_ONLY", code,
                         "the skeleton must never perform the live read")
        self.assertNotIn("import HIT_TEST_ONLY", code)
        self.assertNotIn(".limb_state(", code)
        self.assertIn("limb_state: Optional[Mapping[str, str]] = None", source,
                      "the limb map is an argument, not a lookup")

    def test_limb_state_is_read_only_to_decide_which_hitboxes_exist(self):
        """The L8 guard, stated as the property rather than as a file count.

        It used to assert a single reader in a single file. `lobster.selection`
        became a second one when raycasting landed (DECISIONS.md D25), and it
        asks the identical question — *which capsules exist right now* — for the
        identical reason: reporting a hit or a pick on a limb that is not there
        would be Lobster lying about geometry.

        So the allowlist is named and short, and the assertion that actually
        matters is the second one: **neither reader poses anything.** That is
        the line Scope §5 and L8 draw, and a third reader still fails here.
        """
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        lobster_dir = os.path.join(root, "lobster")
        readers = {}
        for base, _dirs, files in os.walk(lobster_dir):
            for name in sorted(files):
                # octopus_bridge.py *defines* the sentinel and the reader
                if not name.endswith(".py") or name == "octopus_bridge.py":
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
                if "purpose=HIT_TEST_ONLY" in body:
                    readers[name] = body

        self.assertEqual(
            sorted(readers), ["hittest.py", "selection.py"],
            "limb_state may only be read to decide which hitboxes exist "
            "(Scope §5 / L8). A new reader needs a DECISIONS entry saying why "
            "it is asking that question and not an animation one; found "
            "{0}".format(sorted(readers)))

        for name, body in sorted(readers.items()):
            for posing in ("set_pose(", "set_root("):
                self.assertNotIn(
                    posing, body,
                    "{0} both reads limb_state and poses - that is the exact "
                    "collapse L8 forbids".format(name))

    def test_skeleton_has_no_animation_api(self):
        forbidden = ("play", "blend", "transition", "ragdoll", "state_machine",
                     "on_hit", "update")
        for name in forbidden:
            self.assertFalse(
                hasattr(Skeleton, name),
                "Skeleton.{0} exists - animation state, blend trees and ragdoll "
                "transitions are Shrimp's (L8, Scope 12)".format(name))
        mutators = [n for n, m in inspect.getmembers(Skeleton, inspect.isfunction)
                    if n.startswith("set_")]
        self.assertEqual(sorted(mutators), ["set_pose", "set_root"])


if __name__ == "__main__":
    unittest.main()
