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

from lobster.budgets import BudgetViolation
from lobster.cell import CellError, CellManager
from lobster.constants import (MAX_ITEMS_PER_CELL, MAX_RESIDENT_CELLS,
                               PER_PICKED_ITEM_US, SELECTION_PICK_BUDGET_US)
from lobster.events import EventBus
from lobster.geometry import Transform, closest_point_on_segment
from lobster.items import (ItemError, ItemGrid, ItemPlacer, PlacedItem,
                           place_ops,
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

    def test_removing_clears_the_transform_and_nothing_else(self):
        """The location is where the item *went*, and Lobster does not know.
        Clearing it would also restore a mod-placed item to its default
        (Octopus D52, DECISIONS.md D35)."""
        self.place()
        self.manager.remove_item(self.view(), SWORD)
        rec = self.record()
        self.assertFalse(rec.get("world_transform"),
                         "the representation must be gone")
        self.assertEqual(rec.get("current_location_ref"), VILLAGE,
                         "where it went is the caller's to record, not "
                         "Lobster's to guess")

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
                         ["world_transform"],
                         "removal touches the transform only - the location is "
                         "where the item went, which Lobster does not know")

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
                       {"id": SWORD, "default_location_ref": VILLAGE}):
            with self.assertRaises(ItemError) as ctx:
                PlacedItem.from_record(broken)
            self.assertIn("not placed", str(ctx.exception))

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


class TestContentPlacedItems(ItemFixture):
    """The case that could not be expressed before Octopus D52 / D35.

    `current_location_ref` was the only placement field and it was save-only,
    so a mod could not ship a sword on a table at all. Lobster's lint named
    that restriction rather than matching it silently, and Octopus added
    `default_location_ref` in content. This is the round trip that opens up.
    """

    def ship_on_a_table(self, cell_id=VILLAGE, at=(3.0, 1.0, 4.0)):
        """A content package placing an item, with no save layer involved."""
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-heirloom", "type": "Item", "display_name": "Heirloom",
            "default_location_ref": cell_id,
            "world_transform": {"position": list(at),
                                "rotation": [0.0, 0.0, 0.0, 1.0]}}})

    def test_a_mod_placed_item_is_in_the_world_with_no_save_layer(self):
        self.ship_on_a_table()
        found = self.manager.items_in(self.view(), VILLAGE)
        self.assertEqual([i.item_id for i in found], ["item-heirloom"])
        self.assertEqual(found[0].position, (3.0, 1.0, 4.0))

    def test_picking_it_up_does_not_restore_it_to_its_default(self):
        """Clearing the location would fall back to `default_location_ref` and
        put the sword straight back on the table. Removal clears only the
        transform, which is why it works."""
        self.ship_on_a_table()
        self.manager.remove_item(self.view(), "item-heirloom")
        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [])

        record = self.session.resolution().get("item-heirloom")
        self.assertEqual(record.get("default_location_ref"), VILLAGE,
                         "content's placement is not Lobster's to erase")

    def test_the_save_layer_wins_when_it_is_set(self):
        """Octopus D52's whole point: mods compose on the content field, the
        save owns the current one, and 'has the player moved this?' stays
        answerable."""
        self.ship_on_a_table()
        self.manager.remove_item(self.view(), "item-heirloom")
        self.manager.place_item(self.view(), "item-heirloom", FIELD,
                                Transform(position=(9.0, 0.0, 9.0)))

        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [])
        self.assertEqual([i.item_id for i in
                          self.manager.items_in(self.view(), FIELD)],
                         ["item-heirloom"])
        record = self.session.resolution().get("item-heirloom")
        self.assertEqual(record.get("default_location_ref"), VILLAGE)
        self.assertEqual(record.get("current_location_ref"), FIELD)

    def test_a_carried_item_is_in_no_cell(self):
        """Octopus has no inventory record type: carried by X is an item
        located at X. So it buckets under the holder and no cell returns it."""
        self.ship_on_a_table()
        self.session.engine.write({"op": "PATCH", "id": "item-heirloom",
                                   "field": "current_location_ref",
                                   "value": "npc-ada"})
        self.assertEqual(self.manager.items_in(self.view(), VILLAGE), [])
        self.assertEqual(self.manager.items_in(self.view(), FIELD), [])

    def test_nothing_in_lobster_re_derives_the_fallback(self):
        """Octopus D52 names three call sites that read the raw fields and
        warns that a fourth 'would have had to remember the fallback'. Lobster
        is that fourth site, and it goes through `resolve_item_location`.

        What is banned is **re-deriving the fallback**, which is what reading
        `default_location_ref` outside the bridge always means: that field
        exists only as the thing `current_location_ref` falls back to. Reading
        `current_location_ref` *alone* is legitimate and the build lint does
        it - `item_current_location_in_content` is a question about which
        layer set a field, not about where the item ended up.
        """
        import os
        import re
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "lobster")
        offenders = []
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".py") or name == "octopus_bridge.py":
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
                code = "\n".join(
                    line for line in body.splitlines()
                    if not line.lstrip().startswith(("#", "*", ">")))
                code = re.sub(r'""".*?"""', "", code, flags=re.S)
                if 'get("default_location_ref")' in code:
                    offenders.append(name + " (reads the fallback field)")
                for line in code.splitlines():
                    if 'get("current_location_ref")' in line and \
                            "default_location_ref" in line:
                        offenders.append(name + " (re-derives the fallback)")
        self.assertEqual(offenders, [],
                         "these re-derive Octopus's placement fallback instead "
                         "of calling resolve_item_location: {0}".format(
                             offenders))


class TestTheItemCeiling(ItemFixture):
    """A cell full of dropped loot must not silently blow the pick budget
    (DECISIONS.md D36).

    `Selector` tests every item in **every** resident cell on every pick, and
    that scan has no broad phase: items live in records, not in the cell's
    `SpatialIndex`. So the cost is linear and the ceiling is derived from it -
    a declared pick slice, divided by the measured per-item cost and by the
    resident-cell count, because the worst case is all of them full at once.
    """

    def make(self, item_id):
        self.session.engine.write({"op": "CREATE", "record": {
            "id": item_id, "type": "Item", "display_name": item_id}})

    def fill(self, count, cell_id=VILLAGE):
        for i in range(count):
            item_id = "itm-%d" % i
            self.make(item_id)
            self.manager.place_item(self.view(), item_id, cell_id,
                                    Transform(position=(float(i), 0.0, 0.0)))

    def test_the_ceiling_is_derived_from_the_measured_cost(self):
        self.assertEqual(
            MAX_ITEMS_PER_CELL,
            int(SELECTION_PICK_BUDGET_US
                / (PER_PICKED_ITEM_US * MAX_RESIDENT_CELLS)),
            "this number is derived, not chosen - if it is edited directly the "
            "budget stops meaning anything (D20's method)")

    def test_filling_a_cell_past_the_ceiling_is_refused_loudly(self):
        self.fill(MAX_ITEMS_PER_CELL)
        self.make("one-too-many")
        with self.assertRaises(BudgetViolation) as ctx:
            self.manager.place_item(self.view(), "one-too-many", VILLAGE,
                                    Transform(position=(1.0, 0.0, 1.0)))
        violation = ctx.exception
        self.assertEqual(violation.metric, "max_items")
        self.assertEqual(violation.cell_id, VILLAGE)
        self.assertEqual(violation.record_id, "one-too-many")
        self.assertIn("crosshair", str(violation),
                      "the message must say what the ceiling protects")

    def test_a_refusal_leaves_nothing_behind(self):
        """Checked before the write, so there is no half-placed record to
        clean up and no phantom `on_item_placed`."""
        self.fill(MAX_ITEMS_PER_CELL)
        self.make("one-too-many")
        before = len(self.manager.items_in(self.view(), VILLAGE))
        del self.bus.log[:]
        with self.assertRaises(BudgetViolation):
            self.manager.place_item(self.view(), "one-too-many", VILLAGE,
                                    Transform(position=(1.0, 0.0, 1.0)))
        self.assertEqual(len(self.manager.items_in(self.view(), VILLAGE)),
                         before)
        self.assertEqual(self.session.resolution().get("one-too-many")
                         .get("world_transform"), None)
        self.assertEqual([e for e in self.bus.log
                          if e.name.startswith("on_item")], [])

    def test_moving_an_item_already_here_is_not_a_new_one(self):
        """Otherwise a full cell could never rearrange itself."""
        self.fill(MAX_ITEMS_PER_CELL)
        self.manager.place_item(self.view(), "itm-0", VILLAGE,
                                Transform(position=(99.0, 0.0, 99.0)))
        found = {i.item_id: i for i in self.manager.items_in(self.view(),
                                                             VILLAGE)}
        self.assertEqual(found["itm-0"].position, (99.0, 0.0, 99.0))
        self.assertEqual(len(found), MAX_ITEMS_PER_CELL)

    def test_the_ceiling_is_per_cell_not_per_world(self):
        """L6: budgets are declared and enforced per cell."""
        self.fill(MAX_ITEMS_PER_CELL)
        self.make("elsewhere")
        self.manager.place_item(self.view(), "elsewhere", FIELD,
                                Transform(position=(1.0, 0.0, 1.0)))
        self.assertEqual([i.item_id for i in
                          self.manager.items_in(self.view(), FIELD)],
                         ["elsewhere"])

    def test_a_cell_may_declare_a_lower_ceiling_but_never_a_higher_one(self):
        """The existing budget discipline, which items inherit for free."""
        from lobster.budgets import Budget
        self.assertEqual(
            Budget.declared(VILLAGE, {"lobster_budget": {"max_items": 4}})
            .max_items, 4)
        with self.assertRaises(BudgetViolation):
            Budget.declared(VILLAGE, {"lobster_budget": {
                "max_items": MAX_ITEMS_PER_CELL + 1}})


class TestItemGrid(unittest.TestCase):
    """The broad phase that let the ceiling rise (DECISIONS.md D38)."""

    def records(self, *points):
        return [{"id": "itm-%d" % i,
                 "world_transform": {"position": list(p)}}
                for i, p in enumerate(points)]

    def test_it_finds_what_a_linear_scan_finds(self):
        """The only property that matters. A faster broad phase that returns a
        different set is not faster, it is broken."""
        import random
        rng = random.Random(31)
        points = [(rng.uniform(0, 128), 0.0, rng.uniform(0, 128))
                  for _ in range(300)]
        grid = ItemGrid("cell", self.records(*points))
        for _ in range(60):
            start = (rng.uniform(0, 128), 0.0, rng.uniform(0, 128))
            end = (rng.uniform(0, 128), 0.0, rng.uniform(0, 128))
            radius = rng.choice([0.35, 1.0, 2.5])
            found = {i for i, _ in grid.near_segment(start, end, radius)}
            wanted = set()
            for record, point in zip(self.records(*points), points):
                _, d = closest_point_on_segment(start, end, point)
                if d <= radius:
                    wanted.add(record["id"])
            self.assertTrue(wanted <= found,
                            "the grid missed {0}".format(wanted - found))

    def test_an_empty_grid_costs_nothing(self):
        grid = ItemGrid("cell", [])
        self.assertEqual(len(grid), 0)
        self.assertEqual(grid.near_segment((0., 0., 0.), (128., 0., 128.), 2.5),
                         [])

    def test_candidates_are_deduplicated(self):
        """The dilated walk can reach one bucket from two samples."""
        grid = ItemGrid("cell", self.records((5.0, 0.0, 5.0)))
        found = grid.near_segment((0., 0., 0.), (10., 0., 10.), 2.5)
        self.assertEqual([i for i, _ in found], ["itm-0"])

    def test_a_half_placed_record_is_skipped_not_crashed_on(self):
        grid = ItemGrid("cell", [{"id": "broken"},
                                 {"id": "ok", "world_transform":
                                  {"position": [1.0, 0.0, 1.0]}}])
        self.assertEqual(len(grid), 1)

    def test_walk_cost_grows_with_ray_length_not_item_count(self):
        """Which is why the caller has to choose between this and a scan."""
        few = ItemGrid("cell", self.records((1.0, 0.0, 1.0)))
        many = ItemGrid("cell", self.records(*[(float(i), 0.0, 1.0)
                                               for i in range(50)]))
        short = ((0., 0., 0.), (3., 0., 0.))
        long_ = ((0., 0., 0.), (120., 0., 0.))
        self.assertEqual(few.walk_cost(*short, 0.35),
                         many.walk_cost(*short, 0.35))
        self.assertGreater(few.walk_cost(*long_, 0.35),
                           few.walk_cost(*short, 0.35) * 10)

    def test_it_shares_the_traversal_with_the_entity_index(self):
        """Both walk through `spatial.dilated_segment_walk`. That walk has been
        wrong twice (D16, D31) and two copies would mean fixing it twice."""
        import inspect
        from lobster import items, spatial
        self.assertIn("dilated_segment_walk", inspect.getsource(items.ItemGrid))
        self.assertTrue(callable(spatial.dilated_segment_walk))

    def test_it_carries_no_tier_and_no_snapshot_provenance(self):
        """Items are rebuilt from records whenever the resolution changes, so
        they cannot go stale - there is no staleness to attribute, and no tier
        vocabulary to borrow from entities (D18)."""
        grid = ItemGrid("cell", self.records((1.0, 0.0, 1.0)))
        for banned in ("tier", "snapshot_seq", "snapshot_reason", "move",
                       "remove", "refresh_snapshot"):
            self.assertFalse(hasattr(grid, banned), banned)


class TestTheCeilingRose(ItemFixture):

    def test_the_grid_is_what_raised_it(self):
        self.assertEqual(MAX_ITEMS_PER_CELL, 173)
        self.assertGreater(MAX_ITEMS_PER_CELL, 26,
                           "26 was the linear-scan ceiling (D36)")

    def test_a_cell_holds_far_more_than_it_used_to(self):
        for i in range(100):
            self.session.engine.write({"op": "CREATE", "record": {
                "id": "itm-%d" % i, "type": "Item", "display_name": "x"}})
            self.manager.place_item(self.view(), "itm-%d" % i, VILLAGE,
                                    Transform(position=(float(i % 40) * 3.0,
                                                        0.0,
                                                        float(i // 40) * 3.0)))
        self.assertEqual(len(self.manager.items_in(self.view(), VILLAGE)), 100)

    def test_the_grid_and_the_scan_pick_the_same_thing(self):
        """The grid is only used when its walk is cheaper; both paths must
        agree, or a pick would depend on ray length."""
        from lobster.selection import ITEM, Selector
        for i, z in enumerate((4.0, 40.0, 90.0)):
            self.session.engine.write({"op": "CREATE", "record": {
                "id": "itm-%d" % i, "type": "Item", "display_name": "x"}})
            self.manager.place_item(self.view(), "itm-%d" % i, VILLAGE,
                                    Transform(position=(2.0, 0.0, z)))
        view = self.view()
        selector = Selector.from_manager(self.manager, view)
        near = selector.pick((2.0, 0.2, 0.0), (0.0, 0.0, 1.0), 6.0,
                             kinds=(ITEM,), view=view)
        far = selector.pick((2.0, 0.2, 0.0), (0.0, 0.0, 1.0), 120.0,
                            kinds=(ITEM,), view=view)
        self.assertEqual(near.target_id, "itm-0")
        self.assertEqual(far.target_id, "itm-0",
                         "nearest wins on both paths")
