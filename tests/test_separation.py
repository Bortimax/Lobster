"""Section 14 test 1 - terrain/structure separation.

> Terrain/structure separation - never share an object or memory pool.

L2 is the load-bearing half of the first founding decision: the hybrid split is
only worth anything if the two halves stay apart. This test checks the four ways
they could quietly converge - a shared type, a shared object, a shared memory
pool, and a shared code path - because "we kept them separate" is the kind of
claim that is true on the day it is written and false a year later.
"""

from __future__ import annotations

import inspect
import os
import unittest

from lobster import structures as structures_module
from lobster import terrain as terrain_module
from lobster.budgets import (POOL_STRUCTURES, POOL_TERRAIN, BudgetViolation,
                             MemoryLedger)
from lobster.cell import CellManager
from lobster.octopus_bridge import OctopusBridge
from lobster.structures import LiveStructure, StructureVoxelData
from lobster.terrain import Terrain, TerrainCollider, TerrainMesh
from tests.fixtures import (GATEHOUSE, VILLAGE, build_session,
                            standard_workspace)


class TestTerrainStructureSeparation(unittest.TestCase):

    def test_no_shared_type(self):
        """No common base class beyond `object`."""
        terrain_types = (Terrain, TerrainMesh, TerrainCollider)
        structure_types = (StructureVoxelData, LiveStructure)
        for t in terrain_types:
            for s in structure_types:
                shared = set(inspect.getmro(t)) & set(inspect.getmro(s))
                self.assertEqual(
                    shared, {object},
                    "{0} and {1} share base classes {2}; L2 says terrain and "
                    "structures are never the same system".format(
                        t.__name__, s.__name__, shared - {object}))

    def test_no_shared_module_import(self):
        """Neither module imports the other - there is no shared code path."""
        terrain_src = inspect.getsource(terrain_module)
        structures_src = inspect.getsource(structures_module)
        self.assertNotIn("from .structures", terrain_src)
        self.assertNotIn("import structures", terrain_src)
        self.assertNotIn("from .terrain", structures_src)
        self.assertNotIn("import terrain", structures_src)

    def test_terrain_has_no_mutation_api(self):
        """Terrain is static/authored (Scope 0). It cannot be edited at all."""
        forbidden = ("destroy", "destroy_chunks", "set_voxel", "remesh",
                     "damage", "carve", "take_dirty")
        for name in forbidden:
            for cls in (Terrain, TerrainMesh, TerrainCollider):
                self.assertFalse(
                    hasattr(cls, name),
                    "{0}.{1} exists - runtime terrain editing is an explicit "
                    "non-goal (Scope 12), and an API for it is how it "
                    "arrives".format(cls.__name__, name))

    def test_pools_are_disjoint_after_a_real_load(self):
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            view = bridge.frame()
            cell = manager.set_player_cell(view, VILLAGE)

            manager.ledger.assert_pools_disjoint(POOL_TERRAIN, POOL_STRUCTURES)

            terrain_objects = manager.ledger.objects_in_pool(POOL_TERRAIN)
            structure_objects = manager.ledger.objects_in_pool(POOL_STRUCTURES)
            self.assertTrue(terrain_objects)
            self.assertTrue(structure_objects)
            self.assertEqual(terrain_objects & structure_objects, set())

            # and the structure instance is not the terrain object
            live = cell.structure(GATEHOUSE)
            self.assertIsNot(live, cell.terrain)
            self.assertNotEqual(id(live.voxel_data), id(cell.terrain.mesh))

    def test_damaging_a_structure_does_not_touch_terrain(self):
        session = build_session()
        bridge = OctopusBridge(session)
        with standard_workspace() as ws:
            manager = CellManager(ws.path)
            view = bridge.frame()
            cell = manager.set_player_cell(view, VILLAGE)
            before_mesh = cell.terrain.mesh
            before_heights = cell.terrain.collider.heights

            manager.damage_structure(view, VILLAGE, GATEHOUSE, [0, 1])

            self.assertIs(cell.terrain.mesh, before_mesh)
            self.assertEqual(cell.terrain.collider.heights, before_heights)
            self.assertEqual(sorted(cell.structure(GATEHOUSE).destroyed), [0, 1])

    def test_ledger_detects_a_shared_object(self):
        """The check itself works: charging one object to both pools fails."""
        ledger = MemoryLedger()
        shared = object()
        ledger.charge("cell-x", POOL_TERRAIN, 1, obj=shared)
        ledger.charge("cell-x", POOL_STRUCTURES, 1, obj=shared)
        with self.assertRaises(BudgetViolation) as ctx:
            ledger.assert_pools_disjoint()
        self.assertIn("L2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
