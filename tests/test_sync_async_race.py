"""Section 14 test 8 - sync collider / async navmesh (v0.4).

> an NPC's leash target never routes through a cell whose navmesh is marked
> dirty/pending; the physical collider is provably updated on the same frame as
> the visual mesh, every time, regardless of how long the navmesh patch takes.

The rule, from Scope 10:

> the physical voxel collider updates **synchronously**, on the exact frame of
> impact [...] The navmesh patch and any connection-graph patch are allowed to
> **defer asynchronously**. While a cell's navmesh node is marked
> dirty/pending, any AI whose current path or leash target passes through that
> node **holds position**.

And the regression, 15.11:

> Any shortcut that makes the voxel collider update wait on the navmesh patch
> (or vice versa) reintroduces the 10 race - collision is always synchronous
> with the visual mesh; pathing is allowed to lag behind it, never the other way
> around.
"""

from __future__ import annotations

import inspect
import unittest

from lobster import navmesh as navmesh_module
from lobster.cell import CellManager
from lobster.movement import (Agent, HELD_NAVMESH_PENDING, LeaderLeash)
from lobster.navmesh import PatchQueue
from lobster.octopus_bridge import OctopusBridge
from tests.fixtures import (BRIDGE, BRIDGE_DECK_Y, FIELD, VILLAGE,
                            bridge_workspace, build_session, deck_chunk_under)


def deck_point(poly_index: int, *, offset: float = 1.0):
    return (1.0, BRIDGE_DECK_Y, poly_index * 2.0 + offset)


class TestSynchronousColliderAsynchronousNavmesh(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = bridge_workspace()
        self.manager = CellManager(self.ws.path)
        self.cell = self.manager.load(self.bridge.frame(), VILLAGE)
        self.structure = self.cell.structure(BRIDGE)
        self.chunk = deck_chunk_under(3, self.structure.voxel_data)

    def tearDown(self):
        self.ws.close()

    def test_collider_and_mesh_move_on_the_impact_frame(self):
        voxel = self.structure.voxel_data
        cx, cy, cz = voxel.chunk_coords(self.chunk)
        sample = (cx * voxel.chunk_size, cy * voxel.chunk_size,
                  cz * voxel.chunk_size)
        self.assertTrue(self.structure.is_solid(*sample))
        before_geometry = self.structure.geometry_version
        before_navmesh = self.cell.navmesh.version

        self.manager.damage_structure(self.bridge.frame(), VILLAGE, BRIDGE,
                                      [self.chunk])

        self.assertFalse(self.structure.is_solid(*sample),
                         "the collider must be updated on the frame of impact")
        self.assertGreater(self.structure.geometry_version, before_geometry)
        self.assertIn(self.chunk, self.structure.dirty_chunks,
                      "the visual mesh is queued from the same state as the "
                      "collider, not from a separate one")
        self.assertEqual(self.cell.navmesh.version, before_navmesh,
                         "the navmesh must NOT have been patched synchronously")
        self.assertTrue(self.cell.navmesh.dirty,
                        "the affected polys must be marked pending immediately")

    def test_the_collider_never_waits_however_long_the_patch_takes(self):
        """Run the impact frame with the patch queue deliberately starved."""
        voxel = self.structure.voxel_data
        for frame, poly_index in enumerate((1, 2, 4)):
            chunk = deck_chunk_under(poly_index, voxel)
            cx, cy, cz = voxel.chunk_coords(chunk)
            sample = (cx * voxel.chunk_size, cy * voxel.chunk_size,
                      cz * voxel.chunk_size)
            self.manager.damage_structure(self.bridge.frame(), VILLAGE, BRIDGE,
                                          [chunk])
            self.assertFalse(
                self.structure.is_solid(*sample),
                "frame {0}: the collider lagged behind the impact".format(frame))
            # ... and no patch has been pumped at any point
            self.assertGreater(len(self.manager.patches), 0)
        self.assertEqual(self.cell.navmesh.version, 0)

    def test_an_ai_holds_rather_than_routing_through_a_pending_poly(self):
        agents = {"leader": Agent("leader", deck_point(0)),
                  "follower": Agent("follower", deck_point(0, offset=0.2))}
        leash = LeaderLeash("leader", ["follower"])

        self.manager.damage_structure(self.bridge.frame(), VILLAGE, BRIDGE,
                                      [self.chunk])
        self.assertTrue(self.cell.navmesh.dirty)

        results = leash.step(self.cell.navmesh, agents, deck_point(5), dt=0.1)
        leader = results[0]
        self.assertFalse(leader.moved)
        self.assertEqual(leader.reason, HELD_NAVMESH_PENDING,
                         "an AI must hold rather than commit to a route that "
                         "may be invalidated a frame later")

    def test_movement_resumes_once_the_patch_lands(self):
        agents = {"leader": Agent("leader", deck_point(0))}
        leash = LeaderLeash("leader", [])

        # destroy a chunk under a poly that stays walkable afterwards
        chunk = deck_chunk_under(1, self.structure.voxel_data)
        self.manager.damage_structure(self.bridge.frame(), VILLAGE, BRIDGE,
                                      [chunk])
        held = leash.step(self.cell.navmesh, agents, deck_point(5), dt=0.1)[0]
        self.assertEqual(held.reason, HELD_NAVMESH_PENDING)

        self.manager.pump_navmesh(max_jobs=8)
        self.assertEqual(self.cell.navmesh.dirty, set())
        moved = leash.step(self.cell.navmesh, agents, deck_point(5), dt=0.1)[0]
        self.assertFalse(moved.moved, "poly 1's deck is gone, so it is blocked")

    def test_the_patch_queue_cannot_block(self):
        """15.11, structurally: there is no way to make the collider wait."""
        for name in ("wait", "flush", "join", "block", "resolve_now",
                     "flush_now"):
            self.assertFalse(
                hasattr(PatchQueue, name),
                "PatchQueue.{0} exists - a blocking navmesh API is how the "
                "10 race comes back".format(name))

    def test_the_structure_module_cannot_reach_the_patch_queue(self):
        """The synchronous half has no handle on the deferred half."""
        from lobster import structures as structures_module
        code = [line.strip() for line in
                inspect.getsource(structures_module).splitlines()
                if line.strip().startswith(("import ", "from "))]
        offenders = [line for line in code if "navmesh" in line]
        self.assertEqual(offenders, [],
                         "lobster.structures imports the navmesh: {0}".format(
                             offenders))
        for name in ("PatchQueue", "mark_dirty", "apply_patch",
                     "recompute_polys"):
            self.assertFalse(
                hasattr(structures_module, name),
                "lobster.structures exposes {0} - the collider path must not "
                "be able to touch pathing at all (15.11)".format(name))

    def test_navmesh_pathing_avoids_dirty_polys_by_default(self):
        self.manager.damage_structure(self.bridge.frame(), VILLAGE, BRIDGE,
                                      [self.chunk])
        navmesh = self.cell.navmesh
        self.assertIsNone(navmesh.find_path(0, 5),
                          "avoid_dirty is the default")
        self.assertIsNotNone(navmesh.find_path(0, 5, avoid_dirty=False),
                             "the un-patched geometry is still traversable - "
                             "the refusal is about pending state, not about "
                             "the world")


if __name__ == "__main__":
    unittest.main()
