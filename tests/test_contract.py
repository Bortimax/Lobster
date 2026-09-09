"""The cheap contract itself (Scope 13) - the delivery surface, not the tests.

> The cheap contract (13) is part of the delivery surface, not optional
> documentation.

So it gets asserted like one: the exact Event set with the exact payload shapes,
the exact permitted query list, the one record the save integration depends on,
and the three declared invariants. Shrimp and the authoring tool bind against
what this file locks down.
"""

from __future__ import annotations

import os
import unittest

from lobster.events import (CONTRACT_EVENTS, ContractViolation, EventBus,
                            OctopusEventSink, OnEnterCell, OnHitLocation)
from lobster.geometry import Transform
from lobster.octopus_bridge import (ContractError, HIT_TEST_ONLY,
                                    OctopusBridge, PERMITTED_QUERIES,
                                    STRUCTURE_STATE_TYPE, delegates_to_octopus)
from lobster.skeleton import HUMANOID_REGIONS
from lobster.constants import ZONE_SHAPE_PRIMITIVES
from lobster.tiers import ACTIVE, DORMANT, PROJECTILE, TIERS
from tests.fixtures import (FIELD, GATEHOUSE, VILLAGE, build_session)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestEventSurface(unittest.TestCase):
    """"Fire exactly the listed Events with the stated shapes.\""""

    def test_the_event_set_is_exactly_the_scope_list(self):
        self.assertEqual(sorted(CONTRACT_EVENTS), sorted([
            "on_enter_cell", "on_exit_cell", "on_hit_location",
            "on_structure_damaged", "on_interact", "on_item_placed",
            "on_item_removed"]))

    def test_payload_shapes(self):
        bus = EventBus()
        transform = Transform(position=(1.0, 2.0, 3.0))
        shapes = [
            (bus.enter_cell(VILLAGE).to_dict(),
             {"event", "location_id"}),
            (bus.exit_cell(VILLAGE).to_dict(),
             {"event", "location_id"}),
            (bus.hit_location("npc-ada", "head", 3.0, "player").to_dict(),
             {"event", "target_id", "region", "force", "source_id"}),
            (bus.structure_damaged(GATEHOUSE, [1, 2]).to_dict(),
             {"event", "structure_id", "chunk_indices"}),
            (bus.interact("npc-ada").to_dict(),
             {"event", "target_id"}),
            (bus.item_placed("item-sword", VILLAGE, transform).to_dict(),
             {"event", "item_id", "cell_id", "transform"}),
            (bus.item_removed("item-sword", VILLAGE, transform).to_dict(),
             {"event", "item_id", "cell_id", "transform"}),
        ]
        for payload, expected_keys in shapes:
            self.assertEqual(set(payload), expected_keys, payload)

    def test_which_events_lobster_itself_raises(self):
        """CONTRACT §1's "Raised by" column, pinned.

        The heading used to read "Events Lobster fires" and list seven, three of
        which Lobster never fired — the same shape of untrue promise as the
        cross-cell shot (D23). This asserts the split so it cannot drift in
        either direction: a new Lobster-raised event, or one quietly going away.
        """
        import os
        import re
        raised = set()
        root = os.path.join(REPO_ROOT, "lobster")
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".py") or name == "events.py":
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as handle:
                    body = handle.read()
                for method in re.findall(r"bus\.([a-z_]+)\(", body):
                    if "on_" + method in CONTRACT_EVENTS:
                        raised.add("on_" + method)

        self.assertEqual(
            raised,
            set(CONTRACT_EVENTS),
            "the set of events Lobster raises itself has changed; update "
            "CONTRACT §1's 'Raised by' column to match")

        self.assertEqual(
            set(CONTRACT_EVENTS) - raised, set(),
            "**all seven** are Lobster-raised as of D34. D24 recorded three "
            "that were declared and never fired; selection closed "
            "on_interact (D25) and item placement closed the other two. A "
            "regression here means an event went back to being a promise")

    def test_the_caller_raised_events_are_still_fully_wired(self):
        """Declared-but-not-raised is not the same as absent: the payloads,
        the emitters and the Octopus routing all work today."""
        session = build_session()
        bus = EventBus()
        sink = OctopusEventSink(session).attach(bus)
        transform = Transform(position=(1.0, 0.0, 2.0))

        bus.interact("npc-ada")
        bus.item_placed("item-sword", VILLAGE, transform)
        bus.item_removed("item-sword", VILLAGE, transform)

        self.assertEqual([e.name for e in bus.log],
                         ["on_interact", "on_item_placed", "on_item_removed"])
        self.assertIn("on_interact",
                      [f["trigger_type"] for f in sink.fired],
                      "the sink must still route on_interact to Octopus")

    def test_an_event_outside_the_set_cannot_be_subscribed_or_fired(self):
        bus = EventBus()
        with self.assertRaises(ContractViolation):
            bus.subscribe("on_wall_collapsed", lambda e: None)

    def test_a_payload_of_the_wrong_shape_is_refused(self):
        bus = EventBus()
        wrong = OnEnterCell(location_id=VILLAGE)
        object.__setattr__(wrong, "name", "on_hit_location")
        with self.assertRaises(ContractViolation):
            bus.emit(wrong)

    def test_region_is_nullable_for_a_target_with_no_rig(self):
        """DECISIONS.md D16 - null now means "no rig", not "no measurement".

        The wire field stays Optional; what changed is when it is used. Every
        tier with a hitbox now resolves a region.
        """
        bus = EventBus()
        payload = bus.hit_location("npc-bren", None, 9.0, "player").to_dict()
        self.assertIsNone(payload["region"])

    def test_the_region_vocabulary_is_six_and_frozen(self):
        self.assertEqual(len(HUMANOID_REGIONS), 6)
        self.assertEqual(sorted(HUMANOID_REGIONS),
                         ["head", "left_arm", "left_leg", "right_arm",
                          "right_leg", "torso"])

    def test_the_octopus_sink_forwards_only_what_octopus_can_consume(self):
        session = build_session()
        bus = EventBus()
        sink = OctopusEventSink(session).attach(bus)

        bus.enter_cell(VILLAGE)          # residency, not a scene entry
        self.assertEqual(sink.fired, [])

        sink.enter_scene(VILLAGE)        # the player actually arriving
        self.assertEqual(sink.fired[-1]["trigger_type"], "on_enter_scene")

        bus.structure_damaged(GATEHOUSE, [1, 2])
        forwarded = sink.fired[-1]
        self.assertEqual(forwarded["trigger_type"], "on_structure_damaged")
        self.assertNotIn("chunk_indices", forwarded["bindings"],
                         "bindings are record ids; a list has no place in one")


class TestQuerySurface(unittest.TestCase):
    """"Call only the pure live queries listed.\""""

    def test_the_permitted_set_is_exactly_the_scope_list(self):
        self.assertEqual(sorted(PERMITTED_QUERIES), sorted([
            "resolve_npc_state", "zone_occupants", "resolve_equipment_slots",
            "list_active_effects", "limb_state", "structures_in_zone",
            "structures_in_location", "items_in_location"]))

    def test_the_list_grew_deliberately_and_the_reason_is_recorded(self):
        """Eight, not seven. §13's list is closed but not frozen - v0.3 added
        `structures_in_*` to close the §6.5 gap, and §8's item representation
        needs `items_in_location` on the same grounds: a cell cannot represent
        the items in it without asking which those are.

        This assertion exists so the next addition is also an argued one. A
        query appearing here without a DECISIONS entry is the failure mode.
        """
        self.assertEqual(len(PERMITTED_QUERIES), 8)
        self.assertIn("items_in_location", PERMITTED_QUERIES)

    def test_four_of_them_are_octopus_functions_today(self):
        delegates = delegates_to_octopus()
        self.assertTrue(delegates["resolve_npc_state"])
        self.assertTrue(delegates["zone_occupants"])
        self.assertTrue(delegates["resolve_equipment_slots"])
        self.assertTrue(delegates["list_active_effects"])
        # the two structure queries are new in Scope v0.3 and not in lce yet
        self.assertFalse(delegates["structures_in_zone"])
        self.assertFalse(delegates["structures_in_location"])

    def test_only_the_bridge_imports_lce(self):
        """One door into Octopus."""
        allowed = {"octopus_bridge.py", "octopus_path.py", "tiers.py"}
        offenders = []
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, "lobster")):
            for name in sorted(files):
                if not name.endswith(".py") or name in allowed:
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith(("import lce", "from lce")):
                            offenders.append((name, stripped))
        self.assertEqual(
            offenders, [],
            "Octopus is reached through lobster.octopus_bridge and nowhere "
            "else; found {0}".format(offenders))

    def test_a_frame_view_cannot_be_held_across_frames(self):
        session = build_session()
        bridge = OctopusBridge(session)
        view = bridge.frame()
        self.assertEqual(view.structures_in_location(VILLAGE)[0]["id"],
                         GATEHOUSE)
        bridge.close_frame()
        with self.assertRaises(ContractError) as ctx:
            view.structures_in_location(VILLAGE)
        self.assertIn("never held across a frame", str(ctx.exception))

    def test_opening_a_new_frame_closes_the_old_one(self):
        session = build_session()
        bridge = OctopusBridge(session)
        first = bridge.frame()
        bridge.frame()
        self.assertFalse(first.open)

    def test_structure_queries_cost_what_the_scope_declares(self):
        """O(structures in the zone), not O(structures in the world)."""
        session = build_session()
        view = OctopusBridge(session).frame()
        index = view.structure_index()
        self.assertEqual(index.records_scanned_at_build, 1)
        self.assertEqual([r["id"] for r in view.structures_in_zone("zone-village")],
                         [GATEHOUSE])
        self.assertEqual(view.structures_in_location(FIELD), [])

    def test_every_permitted_query_is_callable(self):
        session = build_session()
        with OctopusBridge(session).frame() as view:
            view.resolve_npc_state("npc-ada", lobster_tier=DORMANT)
            view.zone_occupants("zone-village")
            view.resolve_equipment_slots("npc-ada")
            view.list_active_effects("npc-ada")
            view.limb_state("npc-ada", purpose=HIT_TEST_ONLY)
            view.structures_in_zone("zone-village")
            view.structures_in_location(VILLAGE)
            view.items_in_location(VILLAGE)
            self.assertEqual(set(view.calls), set(PERMITTED_QUERIES))


class TestTierVocabulary(unittest.TestCase):

    def test_the_tiers_are_the_scope_three(self):
        self.assertEqual(TIERS, (ACTIVE, PROJECTILE, DORMANT))

    def test_the_middle_tier_does_not_collide_with_octopus(self):
        from lce.npc import TIER_ACTIVE, TIER_DORMANT, TIER_NEARBY
        self.assertEqual(ACTIVE, TIER_ACTIVE)
        self.assertEqual(DORMANT, TIER_DORMANT)
        self.assertNotIn(PROJECTILE, (TIER_ACTIVE, TIER_NEARBY, TIER_DORMANT))

    def test_a_tier_is_per_placement_not_an_entity_class(self):
        """A tier is runtime state on a cell's spatial index, not content.

        Nothing anywhere in Lobster stores a tier on an entity, an archetype or
        a record - `place()` takes it as an argument and `set_tier` changes it.
        A consumer that reads "PROJECTILE-tier mob" as a *kind of creature* has
        misread it; the tier answers "how precisely can this entity be hit right
        now", and the answer changes as the situation does.
        """
        from lobster.spatial import SpatialIndex
        index = SpatialIndex(VILLAGE)
        index.add("mob-1", (4.0, 0.0, 4.0), DORMANT)
        self.assertEqual(index.tier_of("mob-1"), DORMANT)
        index.set_tier("mob-1", PROJECTILE)
        self.assertEqual(index.tier_of("mob-1"), PROJECTILE)
        index.set_tier("mob-1", ACTIVE)
        self.assertEqual(index.tier_of("mob-1"), ACTIVE)

        # and it is not persisted anywhere: a fresh residency starts over
        index.snapshot([("mob-1", (4.0, 0.0, 4.0), DORMANT)])
        self.assertEqual(index.tier_of("mob-1"), DORMANT)

    def test_only_dormant_declares_no_hitbox(self):
        from lobster.tiers import has_hitbox
        self.assertTrue(has_hitbox(ACTIVE))
        self.assertTrue(has_hitbox(PROJECTILE))
        self.assertFalse(has_hitbox(DORMANT),
                         "DORMANT is the tier for 'present in the world but not "
                         "a hit-test candidate' - the cheap one")

    def test_a_hitbox_tier_with_no_rig_fails_loudly(self):
        """§13 invariant 2 - and the failure this replaced was the bad kind.

        A 'lightweight mob' placed at PROJECTILE tier with no Skeleton used to
        be silently unhittable: arrows passed straight through, no error
        anywhere. That is the worst way for an integration mistake to present.
        """
        from lobster.hittest import HitTestError, HitTester
        from lobster.spatial import SpatialIndex
        session = build_session()
        index = SpatialIndex(VILLAGE)
        index.snapshot([("mob-lightweight", (20.0, 0.0, 10.0), PROJECTILE)])
        tester = HitTester(index, {})           # no rigs registered
        with OctopusBridge(session).frame() as view:
            with self.assertRaises(HitTestError) as ctx:
                tester.resolve_projectile(view, (0.0, 1.0, 10.0), (1.0, 0.0, 0.0),
                                          120.0, 9.0)
        message = str(ctx.exception)
        self.assertIn("mob-lightweight", message)
        self.assertIn("silently unhittable", message)
        self.assertIn(DORMANT, message, "the message must name the cheap fix")
        self.assertIn("not an entity class", message)

    def test_a_rigless_mob_at_dormant_tier_is_fine(self):
        """The supported way to have a cheap mob nobody is fighting."""
        from lobster.hittest import HitTester
        from lobster.spatial import SpatialIndex
        session = build_session()
        index = SpatialIndex(VILLAGE)
        index.snapshot([("mob-lightweight", (20.0, 0.0, 10.0), DORMANT)])
        tester = HitTester(index, {})
        with OctopusBridge(session).frame() as view:
            self.assertEqual(
                tester.resolve_projectile(view, (0.0, 1.0, 10.0), (1.0, 0.0, 0.0),
                                          120.0, 9.0), [])


class TestDeclaredInvariants(unittest.TestCase):
    """The three a writer is told they can rely on."""

    def test_one_record_carries_the_save_integration(self):
        self.assertEqual(STRUCTURE_STATE_TYPE, "StructureState")
        session = build_session()
        record = session.resolution().get(GATEHOUSE)
        self.assertEqual(sorted(k for k in record
                                if k in ("id", "location_id",
                                         "destroyed_chunks")),
                         ["destroyed_chunks", "id", "location_id"])
        policy = session.save.schema.get(
            STRUCTURE_STATE_TYPE).fields["destroyed_chunks"].merge_policy
        self.assertEqual(policy, "UNION_TOMBSTONED")

    def test_no_private_save_format_anywhere(self):
        """Invariant 1: geometry is data.

        Nothing in Lobster opens a file for writing at runtime. The build step
        does (that is its job); the runtime package does not.
        """
        offenders = []
        for name in sorted(os.listdir(os.path.join(REPO_ROOT, "lobster"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO_ROOT, "lobster", name),
                      encoding="utf-8") as f:
                for number, line in enumerate(f, 1):
                    if 'open(' in line and any(mode in line
                                               for mode in ('"w', "'w", '"a',
                                                            "'a")):
                        offenders.append("{0}:{1}".format(name, number))
        self.assertEqual(offenders, [],
                         "the runtime must never write a file - break-state is "
                         "an Octopus record (L5); found {0}".format(offenders))

    def test_zone_shape_primitives_are_frozen_at_three(self):
        self.assertEqual(ZONE_SHAPE_PRIMITIVES, ("box", "cylinder", "polygon"))


if __name__ == "__main__":
    unittest.main()
