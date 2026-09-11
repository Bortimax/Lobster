"""Section 14 test 5 - navmesh / connection-graph agreement (v0.3).

> after a severed connection, the navmesh and the connection graph never
> disagree about whether a path exists.

Two systems, one destruction event, and the failure mode is that one of them
notices and the other does not: NPCs pathing across a bridge that is gone, or
refusing to walk through a gate that is fine. The test asserts agreement at
every step - before the destruction, after it, and after the connection is
severed - rather than only at the end, because the interesting disagreements are
the transient ones.
"""

from __future__ import annotations

import unittest

from lobster.cell import CellManager
from lobster.connection_graph import (ConnectionGraphPatcher,
                                      graph_says_connected,
                                      navmesh_says_connected, portal_polys,
                                      sever_ops)
from lobster.geometry import AABB
from lobster.navmesh import (DEFAULT_AGENT_HEIGHT_M, DEFAULT_SUPPORT_PROBE_M,
                             LOAD_BEARING_PROBE_MARGIN_M, NavPoly, Navmesh)
from lobster.octopus_bridge import OctopusBridge
from lobster.structure_state import StructureStateWriter
from tests.fixtures import (BRIDGE, FIELD, KEEP, VILLAGE, bridge_bundle,
                            bridge_workspace, build_session,
                            deck_chunk_under)

REFERENCE_POLY = 0


class TestNavmeshConnectionAgreement(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = bridge_workspace()
        self.manager = CellManager(self.ws.path)
        self.cell = self.manager.load(self.bridge.frame(), VILLAGE)
        self.structure = self.cell.structure(BRIDGE)
        self.patcher = ConnectionGraphPatcher(self.session)

    def tearDown(self):
        self.ws.close()

    def agree_on(self, target: str) -> bool:
        """Assert both systems say the same thing, and return what they said."""
        view = self.bridge.frame()
        navmesh_verdict = navmesh_says_connected(self.cell.navmesh,
                                                 REFERENCE_POLY, target)
        graph_verdict = graph_says_connected(view, VILLAGE, target)
        self.assertIsNotNone(
            navmesh_verdict,
            "this cell's navmesh bakes no portal for {0!r}, so the test is not "
            "actually comparing two opinions".format(target))
        self.assertEqual(
            navmesh_verdict, graph_verdict,
            "navmesh says {0}, connection graph says {1} about {2!r} - the two "
            "systems disagree about whether a path exists".format(
                navmesh_verdict, graph_verdict, target))
        return bool(graph_verdict)

    def collapse_the_bridge(self):
        chunk = deck_chunk_under(3, self.structure.voxel_data)
        writer = StructureStateWriter(self.session, self.manager.bus)
        self.manager.damage_structure(self.bridge.frame(), VILLAGE, BRIDGE,
                                      [chunk], writer=writer)
        return chunk

    def test_they_agree_before_anything_is_destroyed(self):
        self.assertTrue(self.agree_on(FIELD))

    def test_they_agree_after_a_load_bearing_destruction_is_severed(self):
        self.assertTrue(self.agree_on(FIELD))

        chunk = self.collapse_the_bridge()
        self.assertTrue(self.cell.bundle.table_for(BRIDGE).is_load_bearing(chunk))

        # the navmesh patch is deferred; run it, then re-check the connections
        self.assertTrue(self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8))
        severed = self.patcher.check_cell(self.bridge.frame(), VILLAGE,
                                          self.cell.navmesh, REFERENCE_POLY)

        self.assertEqual([s.target_location_id for s in severed], [FIELD])
        self.assertFalse(self.agree_on(FIELD),
                         "both systems must now agree the path is gone")

    def test_both_halves_of_the_connection_are_severed(self):
        """A collapsed bridge is not a one-way passage."""
        self.collapse_the_bridge()
        self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)
        self.patcher.check_cell(self.bridge.frame(), VILLAGE, self.cell.navmesh,
                                REFERENCE_POLY)

        view = self.bridge.frame()
        self.assertFalse(graph_says_connected(view, VILLAGE, FIELD))
        self.assertFalse(graph_says_connected(view, FIELD, VILLAGE))

    def test_severing_uses_delete_entry_with_the_verbatim_entry(self):
        """DECISIONS.md D4 - `connections` is UNION_TOMBSTONED."""
        view = self.bridge.frame()
        ops = sever_ops(view, VILLAGE, FIELD)
        self.assertEqual([op["op"] for op in ops],
                         ["DELETE_ENTRY", "DELETE_ENTRY"])
        self.assertEqual({op["field"] for op in ops}, {"connections"})
        resolved = view.record(VILLAGE)["connections"]
        self.assertIn(ops[0]["value"], resolved,
                      "the entry must be the resolved value verbatim - entry "
                      "identity is canonical whole-value equality")

    def test_a_connection_with_no_baked_portal_is_never_severed(self):
        """No portal polygon means no opinion, not "unreachable" (L4).

        The village's door to the keep interior has no portal in this cell's
        walkway navmesh. Severing it because Lobster cannot see it would delete
        an authored connection over missing geometry.
        """
        self.assertIsNone(navmesh_says_connected(self.cell.navmesh,
                                                 REFERENCE_POLY, KEEP))
        self.collapse_the_bridge()
        self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)
        severed = self.patcher.check_cell(self.bridge.frame(), VILLAGE,
                                          self.cell.navmesh, REFERENCE_POLY)
        self.assertNotIn(KEEP, [s.target_location_id for s in severed])
        self.assertTrue(graph_says_connected(self.bridge.frame(), VILLAGE, KEEP))

    def test_a_non_load_bearing_destruction_touches_neither_system(self):
        """Scope 10: "Destroying a chunk the build step correctly inferred as
        non-load-bearing still never touches either system."
        """
        table = self.cell.bundle.table_for(BRIDGE)
        decorative = next(i for i in self.structure.voxel_data.chunk_indices()
                          if not table.is_load_bearing(i)
                          and not self.structure.voxel_data.chunk_is_empty(i))
        before_version = self.cell.navmesh.version

        result = self.manager.damage_structure(self.bridge.frame(), VILLAGE,
                                               BRIDGE, [decorative])

        self.assertIsNone(result["navmesh_job"])
        self.assertEqual(self.cell.navmesh.dirty, set())
        self.assertEqual(self.cell.navmesh.version, before_version)
        self.assertTrue(self.agree_on(FIELD))

    def test_adjacent_cells_are_named_in_the_patch_job(self):
        """Scope 10.3 - the recompute extends to affected neighbours."""
        chunk = deck_chunk_under(3, self.structure.voxel_data)
        result = self.manager.damage_structure(self.bridge.frame(), VILLAGE,
                                               BRIDGE, [chunk])
        job = result["navmesh_job"]
        self.assertIsNotNone(job)
        self.assertIn(FIELD, job["adjacent_cell_ids"])
        self.assertIn(KEEP, job["adjacent_cell_ids"])

    def test_portal_polys_are_declared_by_the_bake(self):
        portals = portal_polys(self.cell.navmesh)
        self.assertEqual(sorted(portals), [FIELD])


if __name__ == "__main__":
    unittest.main()


class TestTheInferenceCanSeeEverythingThatMattersToWalkability(unittest.TestCase):
    """The build step's load-bearing inference decides *when* to recompute;
    `recompute_polys` decides *what the answer is*. So the inference has to
    reach at least as far as walkability does, or a chunk changes the answer
    without anything asking for it again.

    This was written after a false alarm: a "support from below" false negative
    was reported, tested for, and turned out not to exist - the inference
    already reaches further down than the support probe. What did exist was the
    coupling being accidental. These tests make it a property.
    """

    def poly(self, y=0.0):
        return NavPoly(poly_id=1,
                       points=((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)),
                       y=y)

    def test_the_inference_reaches_at_least_as_far_down_as_the_support_probe(self):
        self.assertGreaterEqual(
            LOAD_BEARING_PROBE_MARGIN_M, DEFAULT_SUPPORT_PROBE_M,
            "a chunk between the support probe and the polygon bound would "
            "hold a polygon up while intersecting nothing - destroy it and "
            "walkability changes with no recompute queued, which is the silent "
            "failure Scope 15.7 forbids")

    def test_the_inference_reaches_the_full_agent_clearance(self):
        """The other half: a chunk in the headroom blocks the polygon."""
        bounds = self.poly().bounds()
        self.assertGreaterEqual(bounds.maximum[1] - self.poly().y,
                                DEFAULT_AGENT_HEIGHT_M)

    def test_a_chunk_that_can_change_walkability_is_always_flagged(self):
        """Swept, rather than argued. Every depth walkability depends on must
        intersect the polygon bound."""
        mesh = Navmesh("cell-x", [self.poly()])
        steps = 24
        for i in range(steps + 1):
            depth = DEFAULT_SUPPORT_PROBE_M * i / steps
            box = AABB((1.0, -depth - 0.01, 1.0), (3.0, -depth, 3.0))
            self.assertTrue(
                mesh.polys_intersecting(box),
                "a chunk %.3f m below the surface supports it but was not "
                "flagged" % depth)
        for i in range(steps + 1):
            height = DEFAULT_AGENT_HEIGHT_M * i / steps
            box = AABB((1.0, height, 1.0), (3.0, height + 0.01, 3.0))
            self.assertTrue(
                mesh.polys_intersecting(box),
                "a chunk %.3f m above the surface blocks it but was not "
                "flagged" % height)

    def test_it_still_stops_somewhere(self):
        """Over-flagging is the safe direction, not a free one: a flag on every
        chunk in the cell would queue a recompute for destroying anything."""
        mesh = Navmesh("cell-x", [self.poly()])
        self.assertFalse(mesh.polys_intersecting(
            AABB((1.0, -8.0, 1.0), (3.0, -6.0, 3.0))))
        self.assertFalse(mesh.polys_intersecting(
            AABB((1.0, 6.0, 1.0), (3.0, 8.0, 3.0))))
        self.assertFalse(mesh.polys_intersecting(
            AABB((40.0, -0.1, 40.0), (42.0, 0.1, 42.0))),
            "a chunk on the far side of the cell is not load-bearing here")


# ---------------------------------------------------------------------------
# The completion, through the path a game actually calls (review L3)
# ---------------------------------------------------------------------------

class TestThePatchCompletesItself(unittest.TestCase):
    """The tests above prove `ConnectionGraphPatcher` works. They call it by
    hand, after `pump_navmesh`, having assembled the invariant themselves - so
    they could not have caught the thing that was actually wrong: **nothing in
    the runtime called it.** A damage job recorded `adjacent_cell_ids` and no
    production caller ever read them.

    These use the public path only. No patcher is constructed here, and no
    connection is severed by the test.
    """

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = bridge_workspace()
        self.addCleanup(self.ws.close)
        # the session reaches the manager, which is what lets it record a
        # severance rather than only decide on one
        self.manager = CellManager(self.ws.path, session=self.session)
        self.cell = self.manager.load(self.bridge.frame(), VILLAGE)

    def collapse(self):
        chunk = deck_chunk_under(3, self.cell.structure(BRIDGE).voxel_data)
        self.manager.damage_structure(
            self.bridge.frame(), VILLAGE, BRIDGE, [chunk],
            writer=StructureStateWriter(self.session, self.manager.bus))
        return chunk

    def agreed(self):
        view = self.bridge.frame()
        poly = self.manager.reference_poly(VILLAGE)
        return (navmesh_says_connected(self.cell.navmesh, poly, FIELD),
                graph_says_connected(view, VILLAGE, FIELD))

    def test_they_agree_before_anything_happens(self):
        self.assertEqual(self.agreed(), (True, True))

    def test_pumping_the_patch_makes_the_graph_agree(self):
        self.collapse()
        self.assertEqual(self.agreed(), (True, True),
                         "the patch is deferred, so nothing has changed yet")

        self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)

        navmesh_verdict, graph_verdict = self.agreed()
        self.assertFalse(navmesh_verdict, "the navmesh still sees a route")
        self.assertFalse(graph_verdict,
                         "the navmesh says the bridge is gone and Octopus "
                         "still says you can walk it")

    def test_the_report_says_what_it_reconciled(self):
        self.collapse()
        done = self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)
        reconciled = done[0]["reconciled"]
        self.assertEqual(reconciled["checked"], [VILLAGE])
        self.assertEqual([s["target_location_id"]
                          for s in reconciled["severed"]], [FIELD])
        self.assertTrue(reconciled["applied"])

    def test_a_neighbour_it_could_not_check_is_named(self):
        """Bounded to the resident ring by 10.3. A neighbour that is not
        resident has no navmesh to ask - which must be reported, not skipped,
        or the gap looks exactly like a clean bill of health."""
        self.collapse()
        done = self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)
        unchecked = done[0]["reconciled"]["unchecked"]
        self.assertIn(FIELD, [u["cell_id"] for u in unchecked])
        self.assertTrue(all(u["reason"] for u in unchecked))

    def test_both_halves_are_severed_without_the_test_touching_octopus(self):
        self.collapse()
        self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)
        view = self.bridge.frame()
        self.assertFalse(graph_says_connected(view, VILLAGE, FIELD))
        self.assertFalse(graph_says_connected(view, FIELD, VILLAGE),
                         "a collapsed bridge is not a one-way passage")

    def test_the_disagreement_does_not_survive_a_reload(self):
        self.collapse()
        self.manager.pump_navmesh(self.bridge.frame(), max_jobs=8)
        self.manager.unload(VILLAGE)

        self.cell = self.manager.load(self.bridge.frame(), VILLAGE)
        navmesh_verdict, graph_verdict = self.agreed()
        self.assertFalse(navmesh_verdict)
        self.assertFalse(graph_verdict,
                         "the two systems disagree again after a round trip")

    def test_destruction_saved_by_an_earlier_session_is_reconciled_on_load(self):
        """The case the test above cannot reach. Once this session has pumped,
        the connection is already severed and a reload would look correct even
        if `load` reconciled nothing.

        So: write the destruction, throw the manager away, and load the cell
        cold - which is what happens when a player quits after collapsing the
        bridge and comes back. Break-state reaches the navmesh on load, so the
        graph has to be re-checked on load.
        """
        chunk = deck_chunk_under(3, self.cell.structure(BRIDGE).voxel_data)
        StructureStateWriter(self.session, self.manager.bus).destroy(
            BRIDGE, [chunk])
        self.assertTrue(graph_says_connected(self.bridge.frame(),
                                             VILLAGE, FIELD),
                        "the connection is already severed, so loading it "
                        "cannot be what severs it")

        fresh = CellManager(self.ws.path, session=self.session)
        cell = fresh.load(self.bridge.frame(), VILLAGE)

        poly = fresh.reference_poly(VILLAGE)
        self.assertFalse(navmesh_says_connected(cell.navmesh, poly, FIELD),
                         "saved break state did not reach the navmesh")
        self.assertFalse(graph_says_connected(self.bridge.frame(),
                                              VILLAGE, FIELD),
                         "the navmesh knows the bridge is gone and Octopus "
                         "still says you can walk it")

    def test_an_intact_cell_severs_nothing_on_load(self):
        """The other direction, and the one that would make this dangerous:
        reconciling on every load must not cut connections that are fine."""
        self.manager.unload(VILLAGE)
        self.manager.load(self.bridge.frame(), VILLAGE)
        self.assertEqual(self.agreed(), (True, True))
        self.assertEqual(self.manager.patcher.report(), [])

    def test_pumping_with_no_view_is_not_possible(self):
        """The half-finished call is what L3 was. It cannot be spelled now."""
        self.collapse()
        with self.assertRaises(TypeError):
            self.manager.pump_navmesh(max_jobs=8)


class TestWhereTheConnectionCheckStandsFrom(unittest.TestCase):
    """`navmesh_says_connected` asks whether an agent can walk from a reference
    polygon to a door, and a hard-coded poly 0 can be the polygon that just
    collapsed - which would report every connection in the cell as severed
    because the reference standing spot is gone."""

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = bridge_workspace()
        self.addCleanup(self.ws.close)
        self.manager = CellManager(self.ws.path, session=self.session)
        self.cell = self.manager.load(self.bridge.frame(), VILLAGE)

    def test_it_is_a_walkable_polygon(self):
        poly = self.manager.reference_poly(VILLAGE)
        self.assertIsNotNone(poly)
        self.assertTrue(self.cell.navmesh.is_walkable(poly))

    def test_it_moves_off_a_polygon_that_has_been_destroyed(self):
        poly = self.manager.reference_poly(VILLAGE)
        self.cell.navmesh.apply_patch({poly: False})
        moved = self.manager.reference_poly(VILLAGE)
        self.assertNotEqual(moved, poly)
        self.assertTrue(self.cell.navmesh.is_walkable(moved))

    def test_a_cell_with_nowhere_to_stand_has_no_opinion(self):
        """Not "every connection is severed". A cell nobody can stand in is a
        geometry problem, not evidence about its doors."""
        navmesh = self.cell.navmesh
        navmesh.apply_patch({pid: False for pid in navmesh.polys})
        self.assertIsNone(self.manager.reference_poly(VILLAGE))

        result = self.manager.reconcile_connections(self.bridge.frame(),
                                                    VILLAGE)
        self.assertEqual(result["severed"], [])
        self.assertIn(VILLAGE, [u["cell_id"] for u in result["unchecked"]])
        self.assertTrue(graph_says_connected(self.bridge.frame(),
                                             VILLAGE, FIELD),
                        "an unwalkable cell severed its connections")

    def spawn_at(self, position):
        """A session whose village Location spawns arrivals at `position`."""
        from tests.fixtures import package_dict
        session = build_session(extra_packages=[package_dict(
            "spawn", [{"op": "PATCH", "id": VILLAGE,
                       "field": "default_spawn_transform",
                       "value": {"position": list(position),
                                 "rotation": [0.0, 0.0, 0.0, 1.0]}}])])
        bridge = OctopusBridge(session)
        manager = CellManager(self.ws.path, session=session)
        cell = manager.load(bridge.frame(), VILLAGE)
        return manager, cell

    def test_it_stands_where_arrivals_arrive(self):
        """The spawn point is the one polygon a content author has declared
        meaningful. It matters when the navmesh is more than one island: the
        lowest-numbered walkable polygon is arbitrary, and asking from the
        wrong island reports a perfectly reachable door as gone - which writes
        a DELETE_ENTRY, so the wrong reference is destructive, not merely
        inaccurate.
        """
        manager, cell = self.spawn_at((1.0, 4.0, 9.0))
        poly = cell.navmesh.poly_at((1.0, 4.0, 9.0))
        self.assertEqual(poly, 4, "the fixture moved; pick another centre")
        self.assertNotEqual(poly, min(cell.navmesh.polys),
                            "the spawn is on the polygon the fallback would "
                            "have chosen anyway, so this proves nothing")
        self.assertEqual(manager.reference_poly(VILLAGE), poly)

    def test_it_falls_back_when_the_spawn_is_off_the_navmesh(self):
        manager, cell = self.spawn_at((900.0, 0.0, 900.0))
        self.assertIsNone(cell.navmesh.poly_at((900.0, 0.0, 900.0)))
        self.assertEqual(manager.reference_poly(VILLAGE),
                         min(cell.navmesh.polys))

    def test_it_falls_back_when_the_spawn_polygon_is_destroyed(self):
        manager, cell = self.spawn_at((1.0, 4.0, 9.0))
        cell.navmesh.apply_patch({4: False})
        poly = manager.reference_poly(VILLAGE)
        self.assertNotEqual(poly, 4)
        self.assertTrue(cell.navmesh.is_walkable(poly))
