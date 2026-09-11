"""Zone volumes, ambient sound, procedural IK, and the world-tick path.

The last of the build order (Scope 4, 3, 6.5). Nothing here is load-bearing for
the section 14 suite, but three of the four are places policy could leak in, so
each one is asserted to stay out:

* a **fourth zone primitive** cannot be added by accident (Scope 4, frozen);
* a **sound list** is read live off the record, never baked (4.5, D7);
* **IK** adjusts a pose it was given and never chooses one (L8);
* the **world tick** writes the same operation a witnessed hit would (6.5).
"""

from __future__ import annotations

import os
import unittest

from lobster.cell import CellManager
from lobster.geometry import Transform
from lobster.ik import (IKResult, apply as apply_ik, solve_foot_placement,
                        terrain_ground_fn, two_bone_ik)
from lobster.octopus_bridge import OctopusBridge
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.sound import audible, parse_sources, sources_for_cell
from lobster.structure_state import StructureStateWriter
from lobster.worldtick import (WorldTickError, occupants_in_blast,
                               placements_for_cell,
                               resolve_blast_in_location,
                               resolve_blast_in_zone)
from lobster.zones import (BOX, CYLINDER, POLYGON, ZoneShapeError,
                           shape_from_dict, volumes_for_cell, zones_containing)
from tests.fixtures import (C, FIELD, GATEHOUSE, KEEP, VILLAGE,
                            build_session, package_dict, standard_workspace)


class TestZoneVolumes(unittest.TestCase):
    """Scope 4 - box, cylinder, polygon, and nothing else, ever."""

    def test_box(self):
        shape = shape_from_dict({"kind": BOX, "center": [0, 0, 0],
                                 "size": [4, 2, 4]})
        self.assertTrue(shape.contains((1.0, 0.5, 1.0)))
        self.assertFalse(shape.contains((3.0, 0.0, 0.0)))

    def test_cylinder(self):
        shape = shape_from_dict({"kind": CYLINDER, "center": [0, 0, 0],
                                 "radius": 3.0, "height": 4.0})
        self.assertTrue(shape.contains((2.0, 1.0, 0.0)))
        self.assertFalse(shape.contains((2.5, 1.0, 2.5)))
        self.assertFalse(shape.contains((0.0, 5.0, 0.0)))

    def test_polygon(self):
        shape = shape_from_dict({"kind": POLYGON,
                                 "points": [[0, 0], [4, 0], [4, 4], [0, 4]],
                                 "min_y": 0.0, "max_y": 3.0})
        self.assertTrue(shape.contains((2.0, 1.0, 2.0)))
        self.assertFalse(shape.contains((5.0, 1.0, 2.0)))
        self.assertFalse(shape.contains((2.0, 4.0, 2.0)))

    def test_a_fourth_primitive_is_refused_by_name(self):
        with self.assertRaises(ZoneShapeError) as ctx:
            shape_from_dict({"kind": "sphere", "center": [0, 0, 0],
                             "radius": 1}, zone_id="zone-x")
        message = str(ctx.exception)
        self.assertIn("zone-x", message)
        self.assertIn("frozen primitives", message)
        self.assertIn("deliberate scope decision", message)

    def test_a_zone_with_no_shape_is_legal(self):
        self.assertIsNone(shape_from_dict(None))

    def test_volumes_come_from_the_record_layer(self):
        session = build_session()
        view = OctopusBridge(session).frame()
        volumes = volumes_for_cell(view, VILLAGE)
        self.assertEqual([v.zone_id for v in volumes], ["zone-village"])
        self.assertEqual(zones_containing(volumes, (64.0, 2.0, 64.0)),
                         ["zone-village"])
        self.assertEqual(zones_containing(volumes, (900.0, 2.0, 900.0)), [])

    def test_shape_location_ref_binds_the_volume_to_one_cell(self):
        """DECISIONS.md D14 - the coordinates mean *that* cell's origin.

        The fixture zone spans the village and the keep interior but declares
        its shape in the village, so the keep gets no volume - which is the
        whole point of the field.
        """
        session = build_session()
        view = OctopusBridge(session).frame()
        self.assertEqual([v.zone_id for v in volumes_for_cell(view, VILLAGE)],
                         ["zone-village"])
        self.assertEqual(volumes_for_cell(view, KEEP), [],
                         "a volume laid out in the village must not appear in "
                         "the keep at the same local coordinates")

    def test_without_shape_location_ref_the_volume_falls_back_to_every_cell(self):
        """The documented fallback, pinned so it cannot drift silently.

        This is the behaviour CONTRACT.md warns authors about: with the field
        unset, one shape appears in every Location the Zone references, each in
        its own local space.
        """
        package = {"format": "lce-package", "package_id": "mod.unbound_zone",
                   "version": "1.0.0", "schema_compat": {"min": 1, "max": 1},
                   "operations": [{"op": "CREATE", "record": {
                       "id": "zone-unbound", "type": "Zone",
                       "display_name": "Unbound",
                       "location_refs": [VILLAGE, KEEP],
                       "shape": {"kind": "box", "center": [4, 1, 4],
                                 "size": [8, 4, 8]}}}]}
        session = build_session(extra_packages=[package])
        view = OctopusBridge(session).frame()
        for cell_id in (VILLAGE, KEEP):
            zone_ids = [v.zone_id for v in volumes_for_cell(view, cell_id)]
            self.assertIn("zone-unbound", zone_ids,
                          "an unbound shape is offered to every cell in "
                          "location_refs")
        # and a Location the Zone does not reference still gets nothing
        self.assertNotIn("zone-unbound",
                         [v.zone_id for v in volumes_for_cell(view, FIELD)])


class TestAmbientSound(unittest.TestCase):
    """Scope 3/4.5 - cell metadata, layered like everything else."""

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)

    def test_sources_are_read_off_the_location_record(self):
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            cell = manager.load(self.bridge.frame(), VILLAGE)
            sources, findings = sources_for_cell(cell)
            self.assertEqual(findings, [])
            self.assertEqual([s.sound_id for s in sources], ["snd-well"])
            self.assertEqual(sources[0].cell_id, VILLAGE)

    def test_a_mod_can_add_a_source_as_ordinary_layered_content(self):
        """The point of UNION_TOMBSTONED here: no bypass channel needed."""
        package = {"format": "lce-package", "package_id": "mod.forge",
                   "version": "1.0.0", "schema_compat": {"min": 1, "max": 1},
                   "operations": [{"op": "MERGE", "id": VILLAGE,
                                   "field": "sound_sources",
                                   "values": [{"position": [20, 0, 20],
                                               "sound_id": "snd-forge",
                                               "radius": 10}]}]}
        session = build_session(extra_packages=[package])
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            cell = manager.load(bridge.frame(), VILLAGE)
            sources, _ = sources_for_cell(cell)
            self.assertEqual(sorted(s.sound_id for s in sources),
                             ["snd-forge", "snd-well"])

    def test_a_malformed_entry_costs_only_that_entry(self):
        sources, findings = parse_sources(
            [{"sound_id": "snd-ok", "position": [0, 0, 0], "radius": 5},
             {"sound_id": "", "position": [0, 0, 0], "radius": 5},
             {"sound_id": "snd-bad-radius", "position": [0, 0, 0],
              "radius": 0}],
            cell_id=VILLAGE)
        self.assertEqual([s.sound_id for s in sources], ["snd-ok"])
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(f["cell_id"] == VILLAGE for f in findings))

    def test_audibility_is_geometry_and_nothing_else(self):
        sources, _ = parse_sources(
            [{"sound_id": "near", "position": [1, 0, 0], "radius": 5},
             {"sound_id": "far", "position": [40, 0, 0], "radius": 5}])
        heard = audible(sources, (0.0, 0.0, 0.0))
        self.assertEqual([s.sound_id for s in heard], ["near"])
        self.assertAlmostEqual(heard[0].falloff_at((0.0, 0.0, 0.0)), 0.8)


class TestProceduralIK(unittest.TestCase):
    """Scope 3 - "on top of skeletal animation", never instead of it."""

    def setUp(self):
        self.skeleton = Skeleton("npc-ada", humanoid_region_set(),
                                 root=Transform(position=(4.0, 2.0, 4.0)))

    def test_feet_are_placed_on_the_ground_beneath_them(self):
        result = solve_foot_placement(self.skeleton, lambda x, z: 2.2)
        self.assertEqual(len(result.feet), 2)
        for foot in result.feet:
            self.assertTrue(foot.grounded_ok)
            self.assertAlmostEqual(foot.grounded[1], 2.2)

    def test_ground_out_of_reach_is_reported_not_snapped_to(self):
        result = solve_foot_placement(self.skeleton, lambda x, z: -50.0)
        self.assertTrue(all(not f.grounded_ok for f in result.feet))
        self.assertEqual(result.pelvis_drop, 0.0)

    def test_the_pelvis_drops_for_the_lower_foot(self):
        def sloped(x, z):
            return 1.8 if x < 4.0 else 2.0
        result = solve_foot_placement(self.skeleton, sloped)
        self.assertLess(result.pelvis_drop, 0.0)
        before = self.skeleton.root.position[1]
        apply_ik(self.skeleton, result)
        self.assertLess(self.skeleton.root.position[1], before)

    def test_solving_does_not_pose_anything_by_itself(self):
        """Applying is a separate, explicit step (L8)."""
        version = self.skeleton.pose_version
        solve_foot_placement(self.skeleton, lambda x, z: 2.0)
        self.assertEqual(self.skeleton.pose_version, version)

    def test_it_uses_the_cells_baked_heightfield(self):
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            cell = manager.load(bridge.frame(), VILLAGE)
            ground = terrain_ground_fn(cell.terrain)
            self.assertEqual(ground(10.0, 10.0), 0.0)

    def test_two_bone_ik_preserves_bone_lengths(self):
        root, joint, effector = (0.0, 2.0, 0.0), (0.0, 1.0, 0.2), (0.0, 0.0, 0.0)
        target = (0.0, 0.3, 0.4)
        new_joint, new_effector = two_bone_ik(root, joint, effector, target)
        from lobster.geometry import distance
        self.assertAlmostEqual(distance(root, new_joint),
                               distance(root, joint), places=5)
        self.assertAlmostEqual(distance(new_joint, new_effector),
                               distance(joint, effector), places=5)

    def test_an_out_of_reach_target_straightens_rather_than_stretching(self):
        root, joint, effector = (0.0, 2.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0)
        new_joint, new_effector = two_bone_ik(root, joint, effector,
                                              (0.0, -50.0, 0.0))
        from lobster.geometry import distance
        self.assertAlmostEqual(distance(root, new_effector), 2.0, places=5)


class TestWorldTickStructureDamage(unittest.TestCase):
    """Scope 6.5 - unwitnessed outcomes resolve once, at event time."""

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()

    def tearDown(self):
        self.ws.close()

    def test_placements_come_from_the_header_alone(self):
        """DECISIONS.md D10 - no payload, no mesh, no cell load."""
        placements = placements_for_cell(self.ws.path, VILLAGE)
        self.assertEqual([p.structure_id for p in placements], [GATEHOUSE])
        self.assertEqual(placements[0].chunk_count, 8)

    def test_a_blast_damages_structures_in_an_unloaded_cell(self):
        writer = StructureStateWriter(self.session)
        result = resolve_blast_in_zone(self.bridge.frame(), self.ws.path,
                                       "zone-village", center=(2.0, 2.0, 14.0),
                                       radius=4.0, writer=writer)
        self.assertIn(GATEHOUSE, result.damaged)
        self.assertTrue(result.damaged[GATEHOUSE])
        record = self.session.resolution().get(GATEHOUSE)
        self.assertEqual(sorted(record["destroyed_chunks"]),
                         sorted(result.damaged[GATEHOUSE]))

    def test_the_operation_is_the_same_one_a_witnessed_hit_writes(self):
        result = resolve_blast_in_location(self.bridge.frame(), self.ws.path,
                                           VILLAGE, center=(2.0, 2.0, 14.0),
                                           radius=4.0)
        self.assertEqual([op["op"] for op in result.ops], ["MERGE"])
        self.assertEqual(result.ops[0]["field"], "destroyed_chunks")
        self.assertEqual(result.ops[0]["id"], GATEHOUSE)

    def test_the_result_is_applied_at_cell_load_with_no_resimulation(self):
        writer = StructureStateWriter(self.session)
        result = resolve_blast_in_zone(self.bridge.frame(), self.ws.path,
                                       "zone-village", center=(2.0, 2.0, 14.0),
                                       radius=4.0, writer=writer)
        manager = CellManager(self.ws.path)
        cell = manager.load(self.bridge.frame(), VILLAGE)
        self.assertEqual(sorted(cell.structure(GATEHOUSE).destroyed),
                         sorted(result.damaged[GATEHOUSE]))

    def test_a_blast_nowhere_near_anything_damages_nothing(self):
        result = resolve_blast_in_location(self.bridge.frame(), self.ws.path,
                                           VILLAGE, center=(500.0, 0.0, 500.0),
                                           radius=2.0)
        self.assertEqual(result.damaged, {})
        self.assertEqual(result.ops, ())

    def test_break_state_without_geometry_is_skipped_and_named(self):
        package = {"format": "lce-package", "package_id": "mod.ghost",
                   "version": "1.0.0", "schema_compat": {"min": 1, "max": 1},
                   "operations": [{"op": "CREATE", "record": {
                       "id": "ghost-tower", "type": "StructureState",
                       "location_id": VILLAGE, "destroyed_chunks": []}}]}
        session = build_session(extra_packages=[package])
        bridge = OctopusBridge(session)
        result = resolve_blast_in_location(bridge.frame(), self.ws.path,
                                           VILLAGE, center=(2.0, 2.0, 14.0),
                                           radius=40.0)
        skipped = [s for s in result.skipped if s["record_id"] == "ghost-tower"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("no authored geometry", skipped[0]["reason"])

    def test_occupants_come_from_octopus_not_from_lobster(self):
        occupants = occupants_in_blast(self.bridge.frame(), "zone-village")
        self.assertIsInstance(occupants, list)

    # -- who is actually in the blast ---------------------------------------
    #
    # The test above asserted a list and the default fixture schedules nobody,
    # so it passed against a reader using two keys Octopus does not produce and
    # returning `[None]` for a zone full of villagers (review L5). An occupancy
    # test whose fixture has no occupants is a test of `list`.

    def peopled(self, *npc_ids):
        """A session with real scheduled occupants in the village zone."""
        ops = []
        for npc_id in npc_ids:
            ops.append(C("sched-" + npc_id, "ScheduleEntry",
                         character_ref=npc_id, location_ref=VILLAGE))
            ops.append({"op": "MERGE", "id": npc_id, "field": "schedule_refs",
                        "values": ["sched-" + npc_id]})
        session = build_session(
            extra_packages=[package_dict("schedule", ops)])
        return OctopusBridge(session)

    def test_an_occupant_comes_back_named(self):
        bridge = self.peopled("npc-ada")
        view = bridge.frame()
        self.assertEqual([o["npc_id"] for o in view.zone_occupants(
            "zone-village")], ["npc-ada"],
            "the fixture schedules nobody, so the assertion below is about an "
            "empty list")
        self.assertEqual(occupants_in_blast(bridge.frame(), "zone-village"),
                         ["npc-ada"])

    def test_it_reports_exactly_who_octopus_reports(self):
        bridge = self.peopled("npc-ada")
        view = bridge.frame()
        want = [o["npc_id"] for o in view.zone_occupants("zone-village")]
        self.assertTrue(want, "nobody is in the zone")
        self.assertEqual(occupants_in_blast(bridge.frame(), "zone-village"),
                         want)

    def test_nobody_comes_back_as_none(self):
        """The shape of the bug: the right number of occupants, none of them
        nameable. A caller targeting Events at that list applies nothing to
        everybody, and nothing raises."""
        occupants = occupants_in_blast(self.peopled("npc-ada").frame(),
                                       "zone-village")
        self.assertNotIn(None, occupants)
        self.assertTrue(all(isinstance(o, str) and o for o in occupants))

    def test_an_occupant_with_no_id_raises_rather_than_joining_the_list(self):
        class Nameless:
            def zone_occupants(self, zone_id, **kwargs):
                return [{"active": True, "present": True}]

        with self.assertRaises(WorldTickError) as ctx:
            occupants_in_blast(Nameless(), "zone-village")
        self.assertIn("npc_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
