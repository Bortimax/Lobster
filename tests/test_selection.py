"""Object selection and raycasting (Scope §8, DECISIONS.md D25).

The first of Scope §8's three, and the one that lets Lobster raise `on_interact`
itself instead of declaring an Event and waiting for somebody to notice
(D24).

The discipline being defended here is L4: selection reports *what is under this
ray*, and nothing about whether it can be used. There is no `is_interactable`
and no ordering by importance — nearest wins, because that is the only ordering
geometry supports.
"""

from __future__ import annotations

import unittest

from lobster.cell import CellManager
from lobster.events import EventBus
from lobster.geometry import Transform
from lobster.octopus_bridge import OctopusBridge
from lobster.selection import (ENTITY, ITEM, PROP, SELECTION_KINDS,
                               STRUCTURE, TERRAIN, Selection,
                               SelectionError, Selector, raymarch_structure,
                               raymarch_terrain)
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.structures import LiveStructure
from lobster.tiers import ACTIVE, DORMANT, PROJECTILE
from tests.fixtures import (FIELD, GATEHOUSE, VILLAGE, build_session,
                            solid_structure, standard_workspace)

DOWN = (0.0, -1.0, 0.0)
NORTH = (0.0, 0.0, 1.0)


class SelectionFixture(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.manager = CellManager(self.ws.path)
        self.view = self.bridge.frame()
        self.manager.set_player_cell(self.view, VILLAGE)
        self.village = self.manager.resident[VILLAGE]
        self.field = self.manager.resident[FIELD]

    def stand(self, cell, entity_id, local, tier=ACTIVE):
        cell.place(entity_id, local, tier,
                   skeleton=Skeleton(entity_id, humanoid_region_set(),
                                     root=Transform(position=local)))

    def selector(self):
        return Selector.from_manager(self.manager, self.view)


class TestPickingEachKind(SelectionFixture):

    def test_a_structure_reports_its_micro_chunk(self):
        """The chunk index is what damage takes, so a pick becomes a hit
        without a second query."""
        found = self.selector().pick((2.0, 2.0, 4.0), NORTH, 40.0)
        self.assertEqual(found.kind, STRUCTURE)
        self.assertEqual(found.target_id, GATEHOUSE)
        self.assertIsNotNone(found.chunk_index)
        live = self.village.structure(GATEHOUSE)
        self.assertLess(found.chunk_index, live.voxel_data.chunk_count)

    def test_a_structure_reports_the_face_it_was_struck_on(self):
        selector = self.selector()
        head_on = selector.pick((2.0, 2.0, 4.0), NORTH, 40.0)
        self.assertEqual(head_on.normal, (0.0, 0.0, -1.0),
                         "a wall hit head-on faces the shooter")
        from_above = selector.pick((2.0, 6.0, 14.0), DOWN, 20.0)
        self.assertEqual(from_above.normal, (0.0, 1.0, 0.0))

    def test_an_entity_reports_its_region(self):
        self.stand(self.village, "ada", (6.0, 0.0, 6.0))
        found = self.selector().pick((6.0, 1.2, 2.0), NORTH, 20.0)
        self.assertEqual((found.kind, found.target_id), (ENTITY, "ada"))
        from lobster.skeleton import HUMANOID_REGIONS
        self.assertIn(found.region, HUMANOID_REGIONS)

    def test_the_ground_can_be_pointed_at(self):
        found = self.selector().pick((40.0, 6.0, 40.0), DOWN, 20.0)
        self.assertEqual(found.kind, TERRAIN)
        self.assertEqual(found.target_id, VILLAGE)
        self.assertAlmostEqual(found.point[1], 0.0, places=2)
        self.assertEqual(found.normal, (0.0, 1.0, 0.0))

    def test_nothing_under_the_ray_is_None(self):
        self.assertIsNone(
            self.selector().pick((40.0, 60.0, 40.0), (0.0, 1.0, 0.0), 20.0))


class TestOnlyOneCellOwnsTheGround(SelectionFixture):
    """`ground_height` clamps at its edges, which is right for foot IK and
    wrong for "which terrain is this". Without a footprint test every resident
    cell answers for every point in the world."""

    def test_each_point_belongs_to_the_cell_it_is_actually_over(self):
        selector = self.selector()
        near = selector.pick((40.0, 6.0, 40.0), DOWN, 20.0)
        far = selector.pick((40.0, 6.0, 160.0), DOWN, 20.0)
        self.assertEqual(near.target_id, VILLAGE)
        self.assertEqual(far.target_id, FIELD,
                         "160 m north is in the field, not the village")

    def test_a_collider_knows_its_own_footprint(self):
        collider = self.village.terrain.collider
        self.assertTrue(collider.covers(40.0, 40.0))
        self.assertFalse(collider.covers(40.0, 400.0))
        self.assertFalse(collider.covers(-40.0, 40.0))

    def test_clamping_still_works_for_the_things_that_want_it(self):
        """IK and the navmesh recompute rely on an edge probe answering."""
        collider = self.village.terrain.collider
        self.assertEqual(collider.ground_height(-5.0, -5.0),
                         collider.ground_height(0.0, 0.0))


class TestNearestWins(SelectionFixture):

    def test_the_nearest_thing_is_returned_whatever_kind_it_is(self):
        self.stand(self.village, "ada", (2.0, 0.0, 8.0))
        found = self.selector().pick((2.0, 1.2, 2.0), NORTH, 40.0)
        self.assertEqual(found.target_id, "ada",
                         "ada at 6 m beats the gatehouse at 10 m")

    def test_pick_all_comes_back_nearest_first(self):
        self.stand(self.village, "ada", (2.0, 0.0, 8.0))
        found = self.selector().pick_all((2.0, 1.2, 2.0), NORTH, 40.0)
        self.assertGreaterEqual(len(found), 2)
        distances = [s.distance for s in found]
        self.assertEqual(distances, sorted(distances))

    def test_kinds_can_be_narrowed(self):
        self.stand(self.village, "ada", (2.0, 0.0, 8.0))
        found = self.selector().pick((2.0, 1.2, 2.0), NORTH, 40.0,
                                     kinds=(STRUCTURE,))
        self.assertEqual(found.kind, STRUCTURE)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(SelectionError):
            self.selector().pick((0.0, 1.0, 0.0), NORTH, 10.0,
                                 kinds=("usable",))


class TestAcrossTheCellBoundary(SelectionFixture):

    def test_you_can_point_at_the_next_valley(self):
        self.stand(self.field, "bandit", (2.0, 0.0, 4.0), PROJECTILE)
        found = self.selector().pick((2.0, 1.2, 100.0), NORTH, 60.0)
        self.assertEqual((found.target_id, found.cell_id), ("bandit", FIELD))

    def test_the_point_comes_back_in_world_space(self):
        self.stand(self.field, "bandit", (2.0, 0.0, 4.0), PROJECTILE)
        found = self.selector().pick((2.0, 1.2, 100.0), NORTH, 60.0)
        self.assertGreater(found.point[2], 128.0,
                           "a point in the field must be past the boundary, "
                           "not in the field's own local coordinates")


class TestBreakStateIsRespected(SelectionFixture):

    def test_a_destroyed_chunk_is_not_pickable(self):
        """A pick has to agree with what is on screen."""
        selector = self.selector()
        before = selector.pick((2.0, 2.0, 4.0), NORTH, 40.0)
        self.assertEqual(before.target_id, GATEHOUSE)

        live = self.village.structure(GATEHOUSE)
        live.destroy_chunks(range(live.voxel_data.chunk_count))

        after = self.selector().pick((2.0, 2.0, 4.0), NORTH, 40.0,
                                     kinds=(STRUCTURE,))
        self.assertIsNone(after, "a flattened structure cannot be pointed at")


class TestWhatSelectionRefusesToDecide(SelectionFixture):
    """L4, structurally."""

    def test_there_is_no_notion_of_interactable(self):
        for banned in ("is_interactable", "usable", "can_use", "priority",
                       "importance"):
            self.assertFalse(hasattr(Selection, banned))
            self.assertFalse(hasattr(Selector, banned))

    def test_a_rigless_entity_is_simply_not_pickable(self):
        """DORMANT carries no rig, so there is nothing to point at - and this
        must not raise the way a hit-test does, because pointing at empty air
        is not an integration bug."""
        self.field.place("mob", (2.0, 0.0, 4.0), DORMANT)
        found = self.selector().pick((2.0, 1.2, 100.0), NORTH, 60.0,
                                     kinds=(ENTITY,))
        self.assertIsNone(found)

    def test_a_severed_limb_is_not_a_target_when_a_view_is_given(self):
        self.stand(self.village, "ada", (2.0, 0.0, 8.0))
        self.session.engine.write({"op": "PATCH", "id": "npc-ada",
                                   "field": "limb_state.left_arm",
                                   "value": "severed"})
        found = self.selector().pick((2.0, 1.2, 2.0), NORTH, 40.0,
                                     kinds=(ENTITY,), view=self.view)
        self.assertIsNotNone(found)
        self.assertNotEqual(found.region, "left_arm")


class TestInteract(SelectionFixture):
    """The gap D24 recorded, closed: Lobster raises `on_interact` itself."""

    def test_it_fires_the_event_for_what_was_pointed_at(self):
        self.stand(self.village, "ada", (6.0, 0.0, 6.0))
        bus = EventBus()
        found = self.selector().interact(bus, (6.0, 1.2, 2.0), NORTH, 20.0)
        self.assertEqual(found.target_id, "ada")
        self.assertEqual([e.to_dict() for e in bus.events_of("on_interact")],
                         [{"event": "on_interact", "target_id": "ada"}])

    def test_pointing_at_nothing_fires_nothing(self):
        bus = EventBus()
        self.assertIsNone(
            self.selector().interact(bus, (40.0, 60.0, 40.0), (0.0, 1.0, 0.0),
                                     20.0))
        self.assertEqual(bus.log, [])

    def test_the_ground_is_not_an_interaction_target_by_default(self):
        """`on_interact(target_id)` cannot say "the ground" usefully - the id
        would be the cell."""
        bus = EventBus()
        found = self.selector().interact(bus, (40.0, 6.0, 40.0), DOWN, 20.0)
        self.assertIsNone(found)
        self.assertEqual(bus.log, [])

    def test_lobster_now_raises_on_interact(self):
        """The counterpart to test_which_events_lobster_itself_raises."""
        import inspect
        from lobster import selection
        self.assertIn("bus.interact(", inspect.getsource(selection))


class TestRaymarchDirectly(unittest.TestCase):

    def test_a_ray_that_misses_the_grid_finds_nothing(self):
        live = LiveStructure(solid_structure("s", grid_size=16))
        self.assertIsNone(
            raymarch_structure(live, (-5.0, 100.0, -5.0), (0.0, 0.0, 1.0), 50.0))

    def test_a_ray_stops_at_the_first_solid_voxel(self):
        live = LiveStructure(solid_structure("s", grid_size=16))
        hit = raymarch_structure(live, (2.0, 2.0, -5.0), (0.0, 0.0, 1.0), 50.0)
        self.assertIsNotNone(hit)
        point, normal, chunk, dist = hit
        self.assertAlmostEqual(point[2], 0.0, places=3,
                               msg="the near face of a solid cube is at z=0")
        self.assertEqual(normal, (0.0, 0.0, -1.0))
        self.assertAlmostEqual(dist, 5.0, places=3)

    def test_range_is_respected(self):
        live = LiveStructure(solid_structure("s", grid_size=16))
        self.assertIsNone(
            raymarch_structure(live, (2.0, 2.0, -5.0), (0.0, 0.0, 1.0), 2.0))

    def test_a_ray_with_no_direction_is_refused(self):
        live = LiveStructure(solid_structure("s", grid_size=16))
        with self.assertRaises(SelectionError):
            raymarch_structure(live, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 10.0)


if __name__ == "__main__":
    unittest.main()


class TestPickingItems(SelectionFixture):
    """Scope §8 step 3: items are a fifth selection kind (D33/D35).

    Not a prop. `PropPlacement` has said so since it was written - *"anything
    the player can pick up, open or be told about is an Octopus `Item` record
    [...] it does not live here"* - and collapsing them would mean `target_id`
    stopped being a record id a caller can resolve.
    """

    def drop(self, item_id="item-sword", cell=None, at=(6.0, 0.0, 6.0)):
        """Place an item, then reopen the frame.

        A `FrameView` holds the resolution it was opened on, so one taken
        before a write cannot see it - which is the "never cached beyond the
        current frame" rule working, not a fixture quirk.
        """
        cell_id = cell or VILLAGE
        self.session.engine.write({"op": "CREATE", "record": {
            "id": item_id, "type": "Item", "display_name": item_id}})
        self.manager.session = self.session
        placed = self.manager.place_item(self.view, item_id, cell_id,
                                         Transform(position=at))
        self.view = self.bridge.frame()
        return placed

    def take(self, item_id="item-sword"):
        self.manager.remove_item(self.view, item_id)
        self.view = self.bridge.frame()

    def test_a_dropped_item_can_be_pointed_at(self):
        self.drop()
        found = self.selector().pick((6.0, 0.2, 2.0), NORTH, 20.0,
                                     view=self.view)
        self.assertEqual((found.kind, found.target_id), (ITEM, "item-sword"))
        self.assertEqual(found.cell_id, VILLAGE)

    def test_the_target_id_is_the_record_id(self):
        """Which is the whole reason items are not props: you can resolve it."""
        self.drop()
        found = self.selector().pick((6.0, 0.2, 2.0), NORTH, 20.0,
                                     kinds=(ITEM,), view=self.view)
        self.assertIsNotNone(self.session.resolution().get(found.target_id))

    def test_a_removed_item_is_no_longer_pickable(self):
        """A pick has to agree with what is on screen."""
        self.drop()
        selector = self.selector()
        self.assertIsNotNone(selector.pick((6.0, 0.2, 2.0), NORTH, 20.0,
                                           kinds=(ITEM,), view=self.view))
        self.take()
        self.assertIsNone(self.selector().pick((6.0, 0.2, 2.0), NORTH, 20.0,
                                               kinds=(ITEM,), view=self.view))

    def test_items_need_a_view_because_they_are_not_baked(self):
        """Unlike props, items live in records. A caller asking about baked
        geometry alone gets no items rather than a crash."""
        self.drop()
        self.assertIsNone(self.selector().pick((6.0, 0.2, 2.0), NORTH, 20.0,
                                               kinds=(ITEM,)))

    def test_an_item_across_the_cell_boundary_is_pickable(self):
        self.drop("item-bow", FIELD, (2.0, 0.0, 4.0))
        found = self.selector().pick((2.0, 0.2, 100.0), NORTH, 60.0,
                                     kinds=(ITEM,), view=self.view)
        self.assertEqual((found.target_id, found.cell_id), ("item-bow", FIELD))
        self.assertGreater(found.point[2], 128.0,
                           "the point must come back in world space")

    def test_nearest_still_wins_across_kinds(self):
        self.drop(at=(2.0, 0.0, 5.0))
        self.stand(self.village, "ada", (2.0, 0.0, 9.0))
        found = self.selector().pick((2.0, 0.3, 2.0), NORTH, 40.0,
                                     view=self.view)
        self.assertEqual(found.target_id, "item-sword",
                         "the sword at 3 m beats ada at 7 m - selection has no "
                         "opinion about which matters (L4)")

    def test_an_item_is_interactable_by_default(self):
        """`interact` covers entities, structures, props and items - the four
        things a player can point at that have an id worth reporting."""
        self.drop()
        bus = EventBus()
        found = self.selector().interact(bus, (6.0, 0.2, 2.0), NORTH, 20.0,
                                         view=self.view)
        self.assertEqual(found.target_id, "item-sword")
        self.assertEqual([e.to_dict() for e in bus.events_of("on_interact")],
                         [{"event": "on_interact", "target_id": "item-sword"}])


class TestTheKindSetGrewDeliberately(unittest.TestCase):

    def test_there_are_five_kinds(self):
        self.assertEqual(sorted(SELECTION_KINDS),
                         ["entity", "item", "prop", "structure", "terrain"])

    def test_items_and_props_are_separate_radii(self):
        """Both are invented numbers - neither declares an extent - so keeping
        them apart means tuning one never silently moves the other."""
        from lobster.selection import ITEM_PICK_RADIUS_M, PROP_PICK_RADIUS_M
        self.assertNotEqual(ITEM_PICK_RADIUS_M, PROP_PICK_RADIUS_M)
