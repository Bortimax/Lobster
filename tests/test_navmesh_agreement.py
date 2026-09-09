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
        self.assertTrue(self.manager.pump_navmesh(max_jobs=8))
        severed = self.patcher.check_cell(self.bridge.frame(), VILLAGE,
                                          self.cell.navmesh, REFERENCE_POLY)

        self.assertEqual([s.target_location_id for s in severed], [FIELD])
        self.assertFalse(self.agree_on(FIELD),
                         "both systems must now agree the path is gone")

    def test_both_halves_of_the_connection_are_severed(self):
        """A collapsed bridge is not a one-way passage."""
        self.collapse_the_bridge()
        self.manager.pump_navmesh(max_jobs=8)
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
        self.manager.pump_navmesh(max_jobs=8)
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
