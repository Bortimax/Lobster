"""Section 14 tests 2 and 9 - break-state as an Octopus record.

**Test 2**

> Structure-break round trip - save -> mod removal (quarantine) -> reinstall ->
> break-state intact.

This is L5 in one scenario: because the only thing that remembers damage is a
`StructureState` record, uninstalling the mod that shipped the structure
quarantines the save operation and reinstalling it brings the damage back, with
no code in Lobster doing anything special. If Lobster had its own save file,
every step of this would be a bespoke migration problem.

**Test 9** (v0.4)

> loading a `StructureState` whose `destroyed_chunks` no longer fit the current
> `grid_size` quarantines only the stale indices and logs why, and never crashes
> the mesh builder or discards the whole record.
"""

from __future__ import annotations

import unittest

from lobster.cell import CellManager
from lobster.events import EventBus
from lobster.octopus_bridge import OctopusBridge
from lobster.structure_state import (StructureStateError,
                                    StructureStateWriter, destroy_op,
                                    repair_ops)
from lobster.structures import LiveStructure, resolve_break_state
from tests.fixtures import (GATEHOUSE, VILLAGE, BundleWorkspace, build_session,
                            package_dict, gatehouse_ops, plain_bundle,
                            solid_structure, standard_workspace, village_bundle)
from tests.fixtures import FIELD, KEEP


def destroyed(session):
    record = session.resolution().get(GATEHOUSE)
    return sorted(record["destroyed_chunks"]) if record else None


class TestBreakStateRoundTrip(unittest.TestCase):
    """Section 14 test 2."""

    def test_save_mod_removal_reinstall(self):
        # --- play: damage the gatehouse ------------------------------------
        session = build_session()
        writer = StructureStateWriter(session, EventBus())
        writer.destroy(GATEHOUSE, [2, 5])
        self.assertEqual(destroyed(session), [2, 5])
        save_data = session.save_data
        self.assertEqual(len(save_data["ops"]), 1)

        # --- the mod that shipped the structure is uninstalled --------------
        removed = build_session(include_gatehouse=False, save_data=save_data)
        report = removed.load_report
        self.assertIsNone(removed.resolution().get(GATEHOUSE))
        self.assertEqual(len(report.quarantined), 1,
                         "the MERGE should be quarantined, not applied")
        self.assertIn("no longer exists", report.quarantined[0]["reason"])
        self.assertEqual(report.quarantined[0]["record_id"], GATEHOUSE)

        # nothing is dropped: the operation is still in the save file verbatim
        self.assertEqual(len(save_data["ops"]), 1)
        self.assertEqual(save_data["ops"][0]["values"], [2, 5])

        # --- the mod is reinstalled ----------------------------------------
        restored = build_session(save_data=save_data)
        self.assertEqual(restored.load_report.quarantined, [])
        self.assertEqual(destroyed(restored), [2, 5],
                         "break-state must survive the round trip intact")

    def test_two_mods_damaging_the_same_keep_both_apply(self):
        """UNION_TOMBSTONED, and the reason Scope 6 chose it.

        > two mods damaging different walls of the same keep should both apply
        """
        mod_a = package_dict("mod.siege_a", [
            {"op": "MERGE", "id": GATEHOUSE, "field": "destroyed_chunks",
             "values": [1]}])
        mod_b = package_dict("mod.siege_b", [
            {"op": "MERGE", "id": GATEHOUSE, "field": "destroyed_chunks",
             "values": [6]}])
        session = build_session(extra_packages=[mod_a, mod_b])
        self.assertEqual(destroyed(session), [1, 6])

        # and a save on top adds to both rather than replacing either
        StructureStateWriter(session).destroy(GATEHOUSE, [3])
        self.assertEqual(destroyed(session), [1, 3, 6])

    def test_a_mod_may_ship_a_pre_ruined_keep(self):
        """`destroyed_chunks` is not save-layer-only (DECISIONS.md D3)."""
        session = build_session(gatehouse_destroyed=[0, 4])
        self.assertEqual(destroyed(session), [0, 4])

    def test_destruction_is_idempotent(self):
        session = build_session()
        writer = StructureStateWriter(session)
        writer.destroy(GATEHOUSE, [2])
        writer.destroy(GATEHOUSE, [2, 2])
        self.assertEqual(destroyed(session), [2])

    def test_repair_is_a_tombstone_reversal_and_can_be_undone(self):
        """Scope 6: "Repair is the rare explicit act, modeled as its own
        tombstone reversal, not a second merge semantics."
        """
        session = build_session()
        writer = StructureStateWriter(session)
        writer.destroy(GATEHOUSE, [2, 5])
        writer.repair(GATEHOUSE, [2])
        self.assertEqual(destroyed(session), [5])
        writer.destroy(GATEHOUSE, [2])
        self.assertEqual(destroyed(session), [2, 5],
                         "a later MERGE must re-destroy a repaired chunk")

    def test_ops_are_the_declared_shapes(self):
        self.assertEqual(destroy_op("s", [3, 1, 1]),
                         {"op": "MERGE", "id": "s", "field": "destroyed_chunks",
                          "values": [1, 3]})
        self.assertEqual(repair_ops("s", [4]),
                         [{"op": "DELETE_ENTRY", "id": "s",
                           "field": "destroyed_chunks", "value": 4}])
        self.assertIsNone(destroy_op("s", []))

    def test_cell_load_applies_the_precomputed_result(self):
        """Scope 6.5: a town fireballed while the player was elsewhere shows up
        already-rubbled on next entry - no re-simulation."""
        session = build_session(gatehouse_destroyed=[1, 2])
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            cell = manager.load(bridge.frame(), VILLAGE)
            live = cell.structure(GATEHOUSE)
            self.assertEqual(sorted(live.destroyed), [1, 2])
            # only the affected micro-chunks are queued for meshing
            self.assertTrue(live.dirty_chunks)
            self.assertLess(len(live.dirty_chunks), live.voxel_data.chunk_count)


class TestBatchDamage(unittest.TestCase):
    """`destroy_many` - the siege-resolution path (Shrimp wishlist 1).

    Many structures updated in one pass, for a town that is not resident. The
    ask was framed as efficiency; the measurements said looping `destroy` was
    already cheap (~7 us a write, lazily resolved once), and that what it
    actually lacked was atomicity and freedom from redundant writes. These
    tests pin both, because those are the properties a siege resolver running
    for game-years depends on.
    """

    def town(self, size: int = 5):
        ops = [{"op": "CREATE",
                "record": {"id": "bld-{0}".format(i), "type": "StructureState",
                           "location_id": VILLAGE, "destroyed_chunks": []}}
               for i in range(size)]
        session = build_session(
            extra_packages=[package_dict("mod.town", ops)])
        bus = EventBus()
        return session, bus, StructureStateWriter(session, bus)

    def test_a_raid_updates_every_building_it_overran(self):
        session, bus, writer = self.town()
        result = writer.destroy_many([("bld-0", [1, 2]), ("bld-1", [3]),
                                      ("bld-4", [0])])
        self.assertEqual(result.structures_damaged(), 3)
        self.assertEqual(result.chunks_destroyed(), 4)
        self.assertEqual(
            sorted(session.resolution().get("bld-0")["destroyed_chunks"]),
            [1, 2])
        self.assertEqual(len(bus.events_of("on_structure_damaged")), 3,
                         "one Event per structure - Scope 13 fixes that shape "
                         "and there is no batch Event")

    def test_a_batch_naming_an_unresolvable_structure_writes_nothing(self):
        """Atomicity: the half-sacked town was the real gap."""
        session, bus, writer = self.town()
        with self.assertRaises(StructureStateError) as ctx:
            writer.destroy_many([("bld-0", [1]), ("bld-1", [2]),
                                 ("ghost-keep", [3]), ("bld-2", [4])])
        self.assertIn("ghost-keep", str(ctx.exception))
        self.assertIn("Nothing has been written", str(ctx.exception))
        self.assertEqual(session.save_data["ops"], [])
        self.assertEqual(bus.log, [])
        for i in range(3):
            self.assertEqual(
                session.resolution().get("bld-{0}".format(i))["destroyed_chunks"],
                [], "no part of a rejected batch may land")

    def test_re_running_a_siege_does_not_grow_the_save(self):
        """L7 - bloat must be traceable to content, never to the shell."""
        session, bus, writer = self.town()
        siege = [("bld-{0}".format(i), [0, 1]) for i in range(5)]
        first = writer.destroy_many(siege)
        for _ in range(9):
            repeat = writer.destroy_many(siege)

        self.assertEqual(first.chunks_destroyed(), 10)
        self.assertEqual(repeat.written, {},
                         "nothing left to knock down")
        self.assertEqual(repeat.skipped["bld-0"], (0, 1),
                         "and it says so, rather than reporting silence")
        self.assertEqual(len(session.save_data["ops"]), 5,
                         "ten runs of the same siege must not append fifty ops")
        self.assertEqual(len(bus.log), 5,
                         "and must not fire fifty Events either")

    def test_partial_overlap_writes_only_what_is_new(self):
        session, bus, writer = self.town()
        writer.destroy_many([("bld-0", [0, 1])])
        result = writer.destroy_many([("bld-0", [1, 2, 3])])
        self.assertEqual(result.written["bld-0"], (2, 3))
        self.assertEqual(result.skipped["bld-0"], (1,))
        self.assertEqual(
            sorted(session.resolution().get("bld-0")["destroyed_chunks"]),
            [0, 1, 2, 3])

    def test_skip_redundant_can_be_turned_off(self):
        session, _bus, writer = self.town()
        writer.destroy_many([("bld-0", [0])])
        writer.destroy_many([("bld-0", [0])], skip_redundant=False)
        self.assertEqual(len(session.save_data["ops"]), 2)

    def test_two_entries_for_one_structure_coalesce(self):
        session, bus, writer = self.town()
        result = writer.destroy_many([("bld-0", [1]), ("bld-0", [2, 1])])
        self.assertEqual(result.written, {"bld-0": (1, 2)})
        self.assertEqual(len(result.ops), 1)
        self.assertEqual(len(bus.events_of("on_structure_damaged")), 1,
                         "one structure taking damage is one Event")

    def test_a_malformed_index_is_rejected_before_anything_is_written(self):
        session, _bus, writer = self.town()
        with self.assertRaises(StructureStateError):
            writer.destroy_many([("bld-0", [1]), ("bld-1", ["two"])])
        self.assertEqual(session.save_data["ops"], [])

    def test_it_needs_no_resident_cell(self):
        """The town being offscreen is the whole point (Scope 6.5)."""
        session, _bus, writer = self.town()
        self.assertFalse(hasattr(writer, "cell"))
        result = writer.destroy_many([("bld-3", [0])])
        self.assertEqual(result.written, {"bld-3": (0,)})

    def test_the_ops_are_the_same_ones_a_single_destroy_writes(self):
        """Batching changes the call shape, never the wire format."""
        session, _bus, writer = self.town()
        result = writer.destroy_many([("bld-0", [2, 1])])
        self.assertEqual(result.ops[0], destroy_op("bld-0", [1, 2]))


class TestOutOfBoundsQuarantine(unittest.TestCase):
    """Section 14 test 9 (v0.4)."""

    def test_only_stale_indices_are_quarantined(self):
        voxel_data = solid_structure(GATEHOUSE, grid_size=16)   # 8 chunks
        self.assertEqual(voxel_data.chunk_count, 8)
        record = {"id": GATEHOUSE, "location_id": VILLAGE,
                  "destroyed_chunks": [1, 7, 99, 4096, -3]}

        resolution = resolve_break_state(voxel_data, record)

        self.assertEqual(sorted(resolution.accepted), [1, 7],
                         "in-range indices must survive")
        self.assertEqual(sorted(q["value"] for q in resolution.quarantined),
                         [-3, 99, 4096])
        for entry in resolution.quarantined:
            self.assertIn("out of range", entry["reason"])
            self.assertIn(GATEHOUSE, entry["reason"] + entry["structure_id"])
            self.assertEqual(entry["record_id"], GATEHOUSE)

    def test_the_mesh_builder_is_never_handed_a_stale_index(self):
        voxel_data = solid_structure(GATEHOUSE, grid_size=16)
        record = {"id": GATEHOUSE, "location_id": VILLAGE,
                  "destroyed_chunks": [0, 999]}
        live = LiveStructure(voxel_data, resolve_break_state(voxel_data, record))

        self.assertEqual(sorted(live.destroyed), [0])
        for index in live.take_dirty():
            # would raise IndexError if a stale index reached the mesher
            self.assertLess(index, voxel_data.chunk_count)
        self.assertFalse(live.is_solid(0, 0, 0), "chunk 0 is gone")
        self.assertTrue(live.is_solid(15, 15, 15), "the rest of the keep stands")

    def test_malformed_entries_quarantine_rather_than_raise(self):
        voxel_data = solid_structure(GATEHOUSE, grid_size=16)
        record = {"id": GATEHOUSE, "location_id": VILLAGE,
                  "destroyed_chunks": [2, "3", None, True]}
        resolution = resolve_break_state(voxel_data, record)
        self.assertEqual(sorted(resolution.accepted), [2])
        self.assertEqual(len(resolution.quarantined), 3)

    def test_a_shrunk_structure_still_loads(self):
        """A package replaces the keep with a smaller one; the save is stale."""
        session = build_session()
        StructureStateWriter(session).destroy(GATEHOUSE, [3, 6])
        bridge = OctopusBridge(session)
        with BundleWorkspace() as ws:
            # rebuilt bundle: grid 8 -> a single micro-chunk, index 0 only
            ws.write(village_bundle(grid_size=8, gate_chunk=0))
            ws.write(plain_bundle(FIELD))
            ws.write(plain_bundle(KEEP))
            manager = CellManager(ws.path)
            cell = manager.load(bridge.frame(), VILLAGE)

            self.assertEqual(cell.structure(GATEHOUSE).destroyed, set())
            reasons = [q["reason"] for q in manager.quarantined()]
            self.assertEqual(len(reasons), 2)
            self.assertTrue(all("shrunk or replaced" in r for r in reasons))
            # the record itself is untouched - only the indices were dropped
            self.assertEqual(sorted(
                session.resolution().get(GATEHOUSE)["destroyed_chunks"]), [3, 6])


if __name__ == "__main__":
    unittest.main()
