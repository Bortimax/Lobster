"""Placing and removing an item's representation (Scope §8, D33/D34).

Step 2 of the §8 build order, and the one that closes D24: `on_item_placed` and
`on_item_removed` were declared, wired and never fired by anything. They are
now the last two of the seven that Lobster originates itself.

The discipline being defended is the same one selection defends (D25): Lobster
moves *where a thing is* and knows nothing about what it is. No pickup, no
inventory, no reachability.
"""

from __future__ import annotations

import unittest

from lobster.cell import CellError, CellManager
from lobster.events import EventBus
from lobster.geometry import Transform
from lobster.items import (ItemError, ItemPlacer, PlacedItem, place_ops,
                           placed_items, remove_ops)
from lobster.octopus_bridge import OctopusBridge, PERMITTED_QUERIES
from tests.fixtures import FIELD, KEEP, VILLAGE, build_session, standard_workspace

SWORD = "item-sword"
SHIELD = "item-shield"


class ItemFixture(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        for item_id, model in ((SWORD, "mdl-sword"), (SHIELD, "mdl-shield")):
            self.session.engine.write({"op": "CREATE", "record": {
                "id": item_id, "type": "Item", "display_name": item_id,
                "model_ref": model}})
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.bus = EventBus()
        self.manager = CellManager(self.ws.path, bus=self.bus,
                                   session=self.session)
        self.manager.set_player_cell(self.view(), VILLAGE)

    def view(self):
        return self.bridge.frame()

    def place(self, item_id=SWORD, cell_id=VILLAGE, at=(12.0, 0.0, 7.0)):
        return self.manager.place_item(self.view(), item_id, cell_id,
                                       Transform(position=at))


class TestTheRoundTrip(ItemFixture):

    def test_a_placed_item_is_in_the_cell_and_out_again(self):
        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [])
        self.place()

        found = self.manager.items_in(self.view(), VILLAGE)
        self.assertEqual([i.item_id for i in found], [SWORD])
        self.assertEqual(found[0].position, (12.0, 0.0, 7.0))
        self.assertEqual(found[0].cell_id, VILLAGE)

        self.manager.remove_item(self.view(), SWORD)
        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [])

    def test_placement_survives_a_reload_because_it_is_a_record(self):
        """L5: Lobster saves nothing. The sword is where it is because the
        `Item` record says so, not because a manager remembered."""
        self.place()
        fresh = CellManager(self.ws.path, session=self.session)
        fresh.set_player_cell(self.view(), VILLAGE)
        self.assertEqual([i.item_id for i in fresh.items_in(self.view(), VILLAGE)],
                         [SWORD])

    def test_model_ref_is_passed_through_untouched(self):
        """Lobster does not resolve it - there is no asset pipeline, and
        inventing one here would be the scope creep L8 forbids."""
        self.place()
        self.assertEqual(
            self.manager.items_in(self.view(), VILLAGE)[0].model_ref,
            "mdl-sword")

    def test_items_land_in_the_cell_they_were_placed_in(self):
        self.place(SWORD, VILLAGE, (1.0, 0.0, 1.0))
        self.place(SHIELD, FIELD, (2.0, 0.0, 2.0))
        self.assertEqual([i.item_id for i in
                          self.manager.items_in(self.view(), VILLAGE)], [SWORD])
        self.assertEqual([i.item_id for i in
                          self.manager.items_in(self.view(), FIELD)], [SHIELD])


class TestTheEvents(ItemFixture):
    """D24's last two, closed."""

    def test_placing_fires_on_item_placed(self):
        self.place()
        fired = [e.to_dict() for e in self.bus.events_of("on_item_placed")]
        self.assertEqual(fired, [{
            "event": "on_item_placed", "item_id": SWORD, "cell_id": VILLAGE,
            "transform": {"position": [12.0, 0.0, 7.0],
                          "rotation": [0.0, 0.0, 0.0, 1.0]}}])

    def test_removing_reports_where_it_was_not_where_it_went(self):
        """Lobster does not know whether it was picked up, destroyed or
        teleported, and guessing would be policy (L4)."""
        self.place()
        del self.bus.log[:]
        self.manager.remove_item(self.view(), SWORD)
        fired = self.bus.events_of("on_item_removed")
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].cell_id, VILLAGE)
        self.assertEqual(fired[0].transform.position, (12.0, 0.0, 7.0))

    def test_removing_something_that_is_not_there_fires_nothing(self):
        """Two systems racing for the same sword is ordinary; the loser gets a
        null, not an exception."""
        self.assertIsNone(self.manager.remove_item(self.view(), SWORD))
        self.assertEqual([e for e in self.bus.log
                          if e.name.startswith("on_item")], [])


class TestTheCoNullInvariant(ItemFixture):
    """CONTRACT §2: both fields, or neither. Never one."""

    def record(self, item_id=SWORD):
        return self.session.resolution().get(item_id)

    def test_placing_sets_both_halves(self):
        self.place()
        rec = self.record()
        self.assertTrue(rec.get("world_transform"))
        self.assertEqual(rec.get("current_location_ref"), VILLAGE)

    def test_removing_clears_both_halves(self):
        self.place()
        self.manager.remove_item(self.view(), SWORD)
        rec = self.record()
        self.assertFalse(rec.get("world_transform"))
        self.assertFalse(rec.get("current_location_ref"))

    def test_the_write_order_never_shows_a_half_placed_item_as_placed(self):
        """Octopus has no multi-field write, so the invariant is held by
        ordering. `current_location_ref` is the commit point in both
        directions, because the index keys on it."""
        ops = place_ops(SWORD, VILLAGE, Transform(position=(1.0, 0.0, 2.0)))
        self.assertEqual([o["field"] for o in ops],
                         ["world_transform", "current_location_ref"],
                         "the transform must land before the location, so the "
                         "intermediate state is 'in no cell' rather than 'in a "
                         "cell at no place'")
        self.assertEqual([o["field"] for o in remove_ops(SWORD)],
                         ["current_location_ref", "world_transform"],
                         "removal drops out of the index first")

    def test_the_ops_are_ordinary_octopus_patches(self):
        """`world_transform` stays a normal record field: a mod, a console
        command or a save editor changing it behaves identically."""
        ops = (place_ops(SWORD, VILLAGE, Transform()) + remove_ops(SWORD))
        for op in ops:
            self.assertEqual(op["op"], "PATCH")
            self.assertEqual(op["id"], SWORD)
            self.assertIn("value", op)

    def test_a_half_placed_record_is_refused_rather_than_guessed_at(self):
        for broken in ({"id": SWORD, "world_transform": {"position": [1, 0, 2]}},
                       {"id": SWORD, "current_location_ref": VILLAGE}):
            with self.assertRaises(ItemError) as ctx:
                PlacedItem.from_record(broken)
            self.assertIn("co-null", str(ctx.exception))

    def test_the_index_skips_a_half_placed_item_rather_than_crashing(self):
        """A save that somehow contains one gets an item that does not appear,
        which is what happened before this subsystem existed - not a crash."""
        self.session.engine.write({"op": "PATCH", "id": SWORD,
                                   "field": "current_location_ref",
                                   "value": VILLAGE})
        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [],
                         "half-placed is not placed")


class TestWhatItRefusesToDo(ItemFixture):
    """L4/L8, structurally - the same line selection holds."""

    def test_placing_into_a_cell_that_is_not_resident_raises(self):
        """The transform is in that cell's coordinates. Placing into a cell
        Lobster cannot see is describing a position it cannot check."""
        self.assertNotIn(KEEP, self.manager.resident)
        with self.assertRaises(CellError) as ctx:
            self.place(SWORD, KEEP)
        self.assertIn("not resident", str(ctx.exception))

    def test_asking_a_non_resident_cell_what_is_in_it_raises(self):
        with self.assertRaises(CellError):
            self.manager.items_in(self.view(), KEEP)

    def test_there_is_no_inventory_surface(self):
        for banned in ("pick_up", "give", "equip", "weight", "stack_count",
                       "can_reach", "is_lootable", "owner"):
            self.assertFalse(hasattr(PlacedItem, banned), banned)
            self.assertFalse(hasattr(ItemPlacer, banned), banned)

    def test_a_transform_is_required_rather_than_coerced(self):
        with self.assertRaises(ItemError):
            self.manager.place_item(self.view(), SWORD, VILLAGE,
                                    (1.0, 0.0, 2.0))

    def test_the_read_is_live_not_cached(self):
        """§13: never cached beyond the current frame. An item somebody else
        moved this frame has moved."""
        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [])
        self.place()
        self.assertEqual(len(self.manager.items_in(self.view(), VILLAGE)), 1)


class TestTheQuery(ItemFixture):
    """`items_in_location` is the eighth permitted query (D34)."""

    def test_it_is_on_the_permitted_list(self):
        self.assertIn("items_in_location", PERMITTED_QUERIES)

    def test_it_is_recorded_as_a_permitted_call(self):
        with self.view() as view:
            view.items_in_location(VILLAGE)
            self.assertIn("items_in_location", view.calls)

    def test_it_costs_items_in_the_cell_not_items_in_the_world(self):
        """The cost model §13 declares for `structures_in_location`, which this
        was added alongside and matches."""
        self.place(SWORD, VILLAGE)
        self.place(SHIELD, FIELD)
        with self.view() as view:
            index = view.item_index()
            self.assertEqual(len(index.in_location(VILLAGE)), 1)
            self.assertEqual(len(index.in_location(FIELD)), 1)
            self.assertGreaterEqual(index.records_scanned_at_build, 2)

    def test_a_closed_view_refuses_it_like_every_other_query(self):
        view = self.view()
        view.close()
        with self.assertRaises(Exception):
            view.items_in_location(VILLAGE)


if __name__ == "__main__":
    unittest.main()
