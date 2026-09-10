"""Reference-counted model residency (ASSET_SCOPE §7 step 3).

A cell's buffers belong to that cell. A *model* is shared — fifty barrels in a
town are fifty placements of one buffer (§2) — so "may this be released" has as
many answers as there are resident cells, and the answer is a count.

ASSET_SCOPE §2 listed five invariants for that count, and each has a test here
under the same words:

1. a model referenced by *n* resident cells is uploaded **once**, not *n* times;
2. releasing one of those cells does **not** release the model;
3. releasing the last one **does**;
4. a count never goes negative, and a release for a model never uploaded is
   counted rather than obeyed;
5. after any sequence of loads and unloads, the set of live models equals the
   set of models the resident cells actually reference.

The fifth is the one that matters, because a refcount is precisely the class of
bug D43 recorded: **every answer stays correct while the memory grows.** So it
is asserted after a randomised walk, against the *backend's* own record of what
it still holds rather than against the counter's opinion of itself.
"""

from __future__ import annotations

import random
import unittest

from lobster.cell import CellManager
from lobster.events import EventBus
from lobster.model_library import ModelLibrary
from lobster.octopus_bridge import OctopusBridge
from lobster.render.recording import RecordingBackend
from lobster.render.residency import GpuResidency
from tests.fixtures import (BundleWorkspace, FIELD, KEEP, VILLAGE,
                            build_session, plain_bundle, primitive_library,
                            prop, village_bundle)

CRATE = "model-crate"
BARREL = "model-barrel"
SIGN = "model-sign"


class ModelResidencyFixture(unittest.TestCase):
    """A three-cell world whose props are declared per test."""

    def world(self, props_by_cell, *, library=None):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = BundleWorkspace()
        self.addCleanup(self.ws.close)
        self.ws.write(village_bundle())
        for cell_id in (FIELD, KEEP):
            self.ws.write(plain_bundle(cell_id,
                                       props=props_by_cell.get(cell_id, ())))
        # the village bundle is written by its own fixture, so its props go on
        # afterwards by rewriting it with them
        if props_by_cell.get(VILLAGE):
            base = village_bundle()
            from dataclasses import replace
            self.ws.write(replace(base,
                                  props=tuple(props_by_cell[VILLAGE])))
        self.ws.write_library(primitive_library()
                              if library is None else library)
        self.bus = EventBus()
        self.manager = CellManager(self.ws.path, bus=self.bus,
                                   session=self.session)
        self.backend = RecordingBackend()
        self.gpu = GpuResidency(self.backend, self.manager).attach(self.bus)

    def view(self):
        return self.bridge.frame()

    def enter(self, cell_id):
        self.manager.set_player_cell(self.view(), cell_id)

    def assert_in_step(self):
        drift = self.gpu.drift()
        self.assertEqual({k: v for k, v in drift.items() if v}, {},
                         "residency and the backend disagree: {0}".format(drift))
        self.assertEqual(self.gpu.live_models(), self.backend.live_models(),
                         "the refcount and the backend disagree about what is "
                         "live - the count is the thing that can lie")


# ---------------------------------------------------------------------------
# What a cell references
# ---------------------------------------------------------------------------

class TestWhatACellReferences(unittest.TestCase):

    def cell(self, props):
        from lobster.budgets import Budget
        from lobster.cell import ResidentCell
        return ResidentCell(plain_bundle(FIELD, props=props),
                            Budget.declared(FIELD, {}))

    def test_distinct_models_not_placements(self):
        """Fifty barrels are one reference. Counting placements would balance
        arithmetically and make the count mean something else."""
        many = [prop("barrel-%d" % i, BARREL) for i in range(50)]
        self.assertEqual(self.cell(many).model_refs(), [BARREL])

    def test_several_models_in_one_cell(self):
        self.assertEqual(
            sorted(self.cell([prop("a", CRATE), prop("b", BARREL),
                              prop("c", CRATE)]).model_refs()),
            [BARREL, CRATE])

    def test_a_prop_with_no_model_is_not_a_reference(self):
        """A prop with an empty `model_ref` is the impostor CONTRACT §10
        describes. Asking the library for "" would turn a documented absence
        into a reported fault."""
        self.assertEqual(self.cell([prop("nameless", "")]).model_refs(), [])

    def test_a_cell_with_no_props_references_nothing(self):
        self.assertEqual(self.cell([]).model_refs(), [])


# ---------------------------------------------------------------------------
# The five invariants
# ---------------------------------------------------------------------------

class TestTheCountedInvariants(ModelResidencyFixture):

    def test_one_upload_however_many_cells_reference_it(self):
        """Invariant 1. The village ring is village + field, and both place a
        crate; one buffer."""
        self.world({VILLAGE: [prop("v", CRATE)], FIELD: [prop("f", CRATE)]})
        self.enter(VILLAGE)
        self.assertIn(FIELD, self.manager.resident, "the fixture must ring")
        self.assertEqual(self.backend.model_uploads.count(CRATE), 1)
        self.assertEqual(self.gpu.model_counts[CRATE], 2)
        self.assertEqual(self.gpu.stats.model_uploads_shared, 1)

    def test_one_upload_however_many_placements(self):
        """Invariant 1 again, from the other direction: fifty barrels."""
        self.world({VILLAGE: [prop("b-%d" % i, BARREL) for i in range(50)]})
        self.enter(VILLAGE)
        self.assertEqual(self.backend.model_uploads.count(BARREL), 1)
        self.assertEqual(self.gpu.model_counts[BARREL], 1)

    def test_releasing_one_of_two_holders_keeps_the_model(self):
        """Invariant 2. One cell goes, the crate stays, because the other cell
        still shows one.

        Driven by `load`/`unload` and not by `set_player_cell`: walking from the
        village to the field keeps *both* resident, which is correct residency
        (D8) and no test of a refcount at all. That was this test's first draft.
        """
        self.world({VILLAGE: [prop("v", CRATE)], FIELD: [prop("f", CRATE)]})
        self.manager.load(self.view(), VILLAGE)
        self.manager.load(self.view(), FIELD)
        self.assertEqual(self.gpu.model_counts[CRATE], 2)
        self.manager.unload(VILLAGE)
        self.assertNotIn(VILLAGE, self.manager.resident)
        self.assertEqual(self.gpu.model_counts[CRATE], 1)
        self.assertIn(CRATE, self.backend.live_models())
        self.assertEqual(self.backend.model_releases.count(CRATE), 0)
        self.assert_in_step()

    def test_releasing_the_last_holder_releases_the_model(self):
        """Invariant 3."""
        self.world({VILLAGE: [prop("v", SIGN)]})
        self.enter(VILLAGE)
        self.assertIn(SIGN, self.backend.live_models())
        self.enter(KEEP)
        self.assertNotIn(VILLAGE, self.manager.resident)
        self.assertEqual(self.backend.model_releases.count(SIGN), 1)
        self.assertEqual(self.backend.live_models(), [])
        self.assert_in_step()

    def test_a_release_for_a_model_never_uploaded_is_counted_not_obeyed(self):
        """Invariant 4, the same shape as `unknown_releases`.

        Reproduced by clearing the counts behind the binding's back — which is
        what a real double-release would look like from in here.
        """
        self.world({VILLAGE: [prop("v", CRATE)]})
        self.enter(VILLAGE)
        self.gpu.model_counts.clear()
        self.enter(KEEP)
        self.assertEqual(self.gpu.stats.unknown_model_releases, 1)
        self.assertEqual(self.backend.model_releases.count(CRATE), 0,
                         "obeying it would take a live model away from a cell "
                         "that is still showing one")

    def test_a_count_never_goes_negative(self):
        """Invariant 4's other half. A key exists exactly while the model is
        live, so there is no zero to go below - asserted rather than assumed."""
        self.world({VILLAGE: [prop("v", CRATE)], FIELD: [prop("f", CRATE)]})
        for cell_id in (VILLAGE, FIELD, KEEP, VILLAGE, KEEP):
            self.enter(cell_id)
            self.assertTrue(all(n > 0 for n in self.gpu.model_counts.values()),
                            self.gpu.model_counts)

    def test_the_live_set_equals_what_the_resident_cells_reference(self):
        """Invariant 5, over a randomised walk.

        The one that catches a refcount, because a leaked model draws perfectly
        (D43) and every *answer* stays correct while the memory grows.
        """
        self.world({VILLAGE: [prop("v1", CRATE), prop("v2", BARREL)],
                    FIELD: [prop("f1", CRATE)],
                    KEEP: [prop("k1", SIGN), prop("k2", BARREL)]})
        rng = random.Random(7)
        for step in range(60):
            self.enter(rng.choice([VILLAGE, FIELD, KEEP]))
            expected = sorted({ref for cell in self.manager.resident.values()
                               for ref in cell.model_refs()})
            self.assertEqual(self.backend.live_models(), expected,
                             "step {0}: the backend holds {1}, the resident "
                             "cells reference {2}".format(
                                 step, self.backend.live_models(), expected))
            self.assert_in_step()
        self.assertGreater(self.gpu.stats.model_releases, 0,
                           "the walk never released anything, so it never "
                           "tested the thing it is for")

    def test_nothing_is_left_behind_when_the_world_empties(self):
        """The end state a leak hides in: unload everything, hold nothing."""
        self.world({VILLAGE: [prop("v", CRATE)], FIELD: [prop("f", BARREL)],
                    KEEP: [prop("k", SIGN)]})
        self.enter(VILLAGE)
        self.enter(KEEP)
        for cell_id in list(self.manager.resident):
            self.manager.unload(cell_id)
        self.assertEqual(self.backend.live_models(), [])
        self.assertEqual(self.gpu.model_counts, {})
        self.assertEqual(self.gpu.retained_by, {})
        self.assert_in_step()


class TestItemsAreCountedToo(unittest.TestCase):
    """D48 deferred this to step 5; D50 is where it landed.

    A prop is baked into the bundle and fixed for the whole residency. An item
    is a record and can be dropped or picked up mid-residency, so its model's
    lifetime is not the cell's - which is why `on_item_placed` and
    `on_item_removed` are two more subscribers rather than a cache somebody has
    to remember to invalidate.
    """

    def setUp(self):
        from lobster.geometry import Transform
        self.Transform = Transform
        self.session = build_session()
        for record in ({"id": "item-sword", "type": "Item",
                        "display_name": "Sword", "model_ref": CRATE},
                       {"id": "item-shield", "type": "Item",
                        "display_name": "Shield", "model_ref": CRATE},
                       {"id": "item-lamp", "type": "Item",
                        "display_name": "Lamp", "model_ref": SIGN},
                       {"id": CRATE, "type": "Model"},
                       {"id": SIGN, "type": "Model"}):
            self.session.engine.write({"op": "CREATE", "record": record})
        self.bridge = OctopusBridge(self.session)
        self.ws = BundleWorkspace()
        self.addCleanup(self.ws.close)
        self.ws.write(village_bundle())
        for cell_id in (FIELD, KEEP):
            self.ws.write(plain_bundle(cell_id))
        self.ws.write_library(primitive_library())
        self.bus = EventBus()
        self.manager = CellManager(self.ws.path, bus=self.bus,
                                   session=self.session)
        self.backend = RecordingBackend()
        self.gpu = GpuResidency(self.backend, self.manager).attach(self.bus)
        self.manager.load(self.bridge.frame(), VILLAGE)

    def place(self, item_id, cell_id=VILLAGE, position=(6.0, 0.0, 8.0)):
        self.manager.place_item(self.bridge.frame(), item_id, cell_id,
                                self.Transform(position=position))

    def remove(self, item_id):
        self.manager.remove_item(self.bridge.frame(), item_id)

    def assert_in_step(self):
        drift = self.gpu.drift()
        self.assertEqual({k: v for k, v in drift.items() if v}, {}, drift)

    def test_placing_an_item_uploads_its_model(self):
        self.assertEqual(self.backend.live_models(), [])
        self.place("item-sword")
        self.assertEqual(self.backend.live_models(), [CRATE])
        self.assert_in_step()

    def test_removing_the_last_item_releases_it(self):
        self.place("item-sword")
        self.remove("item-sword")
        self.assertEqual(self.backend.live_models(), [])
        self.assertEqual(self.backend.model_releases.count(CRATE), 1)
        self.assert_in_step()

    def test_two_items_sharing_a_model_upload_it_once(self):
        self.place("item-sword", position=(6.0, 0.0, 8.0))
        self.place("item-shield", position=(7.0, 0.0, 8.0))
        self.assertEqual(self.backend.model_uploads.count(CRATE), 1)
        self.remove("item-sword")
        self.assertEqual(self.backend.live_models(), [CRATE],
                         "the shield still needs it")
        self.remove("item-shield")
        self.assertEqual(self.backend.live_models(), [])
        self.assert_in_step()

    def test_a_prop_and_an_item_sharing_a_model_are_two_references(self):
        """One buffer, two holders, and neither release takes it early.

        Built on its own bus: attaching a second `GpuResidency` to the fixture's
        bus made the first one see an `on_enter_cell` for a cell that was not
        resident in *its* manager, which is a loud failure and rightly so.
        """
        from dataclasses import replace
        self.manager.unload(VILLAGE)
        self.ws.write(replace(village_bundle(),
                              props=(prop("crate-1", CRATE),)))
        bus = EventBus()
        manager = CellManager(self.ws.path, bus=bus, session=self.session)
        backend = RecordingBackend()
        gpu = GpuResidency(backend, manager).attach(bus)

        manager.load(self.bridge.frame(), VILLAGE)
        self.assertEqual(gpu.model_counts[CRATE], 1, "the prop")
        manager.place_item(self.bridge.frame(), "item-sword", VILLAGE,
                           self.Transform(position=(6.0, 0.0, 8.0)))
        self.assertEqual(gpu.model_counts[CRATE], 1,
                         "a prop and an item in one cell are one reference - "
                         "the count is per distinct model per cell")
        self.assertEqual(backend.model_uploads.count(CRATE), 1,
                         "the item re-uploaded a model the prop already had")
        manager.remove_item(self.bridge.frame(), "item-sword")
        self.assertEqual(backend.live_models(), [CRATE],
                         "the prop still needs it")
        manager.unload(VILLAGE)
        self.assertEqual(backend.live_models(), [])

    def test_the_cell_going_releases_what_its_items_held(self):
        self.place("item-sword")
        self.place("item-lamp", position=(8.0, 0.0, 8.0))
        self.assertEqual(self.backend.live_models(), sorted([CRATE, SIGN]))
        self.manager.unload(VILLAGE)
        self.assertEqual(self.backend.live_models(), [])
        self.assertEqual(self.gpu.model_counts, {})

    def test_an_item_in_a_cell_that_is_not_resident_holds_nothing(self):
        """`place_item` refuses one anyway, so this is the reverse: an item
        already in a record for a cell nobody has loaded."""
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-far", "type": "Item", "display_name": "Far",
            "model_ref": SIGN, "default_location_ref": KEEP,
            "world_transform": {"position": [1.0, 0.0, 1.0],
                                "rotation": [0, 0, 0, 1]}}})
        self.assertEqual(self.backend.live_models(), [])
        self.assert_in_step()

    def test_loading_a_cell_picks_up_the_items_already_in_it(self):
        """An item placed before the cell was resident still gets its model
        when the cell loads - the sync is a set difference, not an event log."""
        self.place("item-sword")
        self.manager.unload(VILLAGE)
        self.assertEqual(self.backend.live_models(), [])
        self.manager.load(self.bridge.frame(), VILLAGE)
        self.assertEqual(self.backend.live_models(), [CRATE])
        self.assert_in_step()

    def test_an_item_with_no_model_ref_holds_nothing(self):
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-plain", "type": "Item", "display_name": "Plain"}})
        self.place("item-plain")
        self.assertEqual(self.backend.live_models(), [])
        self.assert_in_step()

    def test_churn_leaves_nothing_behind(self):
        """The randomised walk, with items moving as well as cells."""
        rng = random.Random(11)
        items = ["item-sword", "item-shield", "item-lamp"]
        placed = set()
        for _step in range(40):
            item = rng.choice(items)
            if item in placed:
                self.remove(item)
                placed.discard(item)
            else:
                self.place(item, position=(4.0 + rng.random(), 0.0, 8.0))
                placed.add(item)
            self.assert_in_step()
        for item in list(placed):
            self.remove(item)
        self.assertEqual(self.backend.live_models(), [])

    def test_a_manager_with_no_session_answers_no_item_models(self):
        """The same manager that cannot place an item either. Documented, not
        silent: `model_refs_for` still answers for props."""
        bare = CellManager(self.ws.path)
        self.assertEqual(bare.item_model_refs(VILLAGE), [])


class TestDriftSeesModels(ModelResidencyFixture):
    """`drift()` must compare the counts against the *world*, not against
    themselves.

    Written after a mutation survived: replacing the recomputed `needed` set
    with `set(self.model_counts)` made the model half of `drift()` compare a
    number to itself, always agree, and pass every test there was. A leak check
    that cannot report a leak is the D30 shape - a guard nothing can make fire.
    """

    def test_a_model_no_resident_cell_references_is_a_leak(self):
        self.world({VILLAGE: [prop("v", CRATE)]})
        self.enter(VILLAGE)
        self.assertEqual(self.gpu.drift()["models_leaked"], [])
        self.gpu.model_counts[SIGN] = 1          # held, referenced by nobody
        self.assertEqual(self.gpu.drift()["models_leaked"], [SIGN])

    def test_a_referenced_model_that_is_not_live_is_missing(self):
        self.world({VILLAGE: [prop("v", CRATE)]})
        self.enter(VILLAGE)
        self.assertEqual(self.gpu.drift()["models_missing"], [])
        del self.gpu.model_counts[CRATE]
        self.assertEqual(self.gpu.drift()["models_missing"], [CRATE])

    def test_in_step_is_false_when_either_is_wrong(self):
        self.world({VILLAGE: [prop("v", CRATE)]})
        self.enter(VILLAGE)
        self.assertTrue(self.gpu.in_step())
        self.gpu.model_counts[BARREL] = 1
        self.assertFalse(self.gpu.in_step())


# ---------------------------------------------------------------------------
# A model that is not there
# ---------------------------------------------------------------------------

class TestAModelTheLibraryDoesNotHave(ModelResidencyFixture):

    def test_it_is_named_rather_than_swallowed(self):
        """The build lint refuses this world (`prop_model_ref_unresolved`), so
        reaching here means a bundle and a library that were built apart. The
        symptom is otherwise a barrel that is simply not there."""
        self.world({VILLAGE: [prop("v", "model-ghost")]})
        self.enter(VILLAGE)
        self.assertEqual(self.gpu.stats.missing_models, 1)
        self.assertEqual(self.gpu.unresolved_models(),
                         {"model-ghost": [VILLAGE]})
        self.assertEqual(self.backend.model_uploads, [])

    def test_it_is_not_drift(self):
        """A ref the library cannot satisfy can never be live, so counting it
        as missing would make a content typo look like a permanent leak."""
        self.world({VILLAGE: [prop("v", "model-ghost")]})
        self.enter(VILLAGE)
        self.assert_in_step()

    def test_it_is_forgotten_when_the_cell_goes(self):
        self.world({VILLAGE: [prop("v", "model-ghost")]})
        self.enter(VILLAGE)
        self.enter(KEEP)
        self.assertEqual(self.gpu.unresolved_models(), {})

    def test_a_world_with_no_library_at_all_still_loads(self):
        """Every world built before the library existed has none, and it is a
        derived artifact that is safe to delete by definition (D1)."""
        self.world({VILLAGE: [prop("v", CRATE)]}, library=ModelLibrary())
        self.enter(VILLAGE)
        self.assertIn(VILLAGE, self.manager.resident)
        self.assertEqual(self.gpu.stats.missing_models, 1)
        self.assert_in_step()


class TestTheLibraryIsReadOnce(ModelResidencyFixture):

    def test_it_is_cached_across_loads(self):
        self.world({VILLAGE: [prop("v", CRATE)]})
        first = self.manager.library
        self.enter(VILLAGE)
        self.assertIs(self.manager.library, first)

    def test_an_absent_file_is_an_empty_library_not_an_error(self):
        import os
        self.world({VILLAGE: [prop("v", CRATE)]})
        os.remove(self.manager.library_path())
        self.manager._library = None
        self.assertEqual(self.manager.library.model_refs(), [])

    def test_a_corrupt_library_is_loud_and_names_the_file(self):
        from lobster.cell import CellError
        self.world({VILLAGE: [prop("v", CRATE)]})
        with open(self.manager.library_path(), "r+b") as handle:
            handle.write(b"NOTALIB!")
        self.manager._library = None
        with self.assertRaises(CellError) as ctx:
            self.manager.library
        self.assertIn("bad magic", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
