"""The build step: `.vox` in, `.lobster_cell` out (Scope 4.5, 10, 15.2).

Four things get asserted here, in rough order of how much damage they do when
they break:

1. the build-step **lint** refuses a world that cannot work - a one-way
   connection, an unenterable Location, a smuggled sound list;
2. `navmesh_load_bearing` is **inferred**, not authored (Scope 10, v0.4), and
   an author's override is honoured and reported;
3. terrain and structures are meshed by **two separate meshers** (L2, 15.1),
   and both produce what they claim;
4. the whole pipeline round-trips: build a world, load a cell out of it, and
   find the same geometry.
"""

from __future__ import annotations

import os
import unittest

from lobster.build.builder import BuildError, build_from_file, build_world
from lobster.build.lighting import bake_lightmap
from lobster.build.lint import check_item_placements, ERROR_CODES, errors, lint_world
from lobster.build.manifest import ManifestError, load_manifest
from lobster.build.navmesh_bake import bake_navmesh, unreachable_portals
from lobster.build.navmesh_inference import (infer_load_bearing,
                                             inference_report,
                                             validate_overrides)
from lobster.build.terrain_mesher import (ColumnField, column_field_from_vox,
                                          flat_column_field, mesh_terrain)
from lobster.build.vox import VoxError, cube_side, read_vox, to_structure
from lobster.bundle import read_bundle
from lobster.cell import CellManager
from lobster.geometry import Transform
from lobster.navmesh import Navmesh, NavPoly
from lobster.octopus_bridge import OctopusBridge, content_view
from lobster.structures import StructureVoxelData
from tests.fixtures import (BuildWorkspace, cube_vox, demo_manifest_cells,
                            demo_world_ops, slab_vox, write_vox_file)


def build_demo(ws: BuildWorkspace, *, ops_kwargs=None, cells=None,
               write: bool = True):
    slab_vox(os.path.join(ws.art, "slab.vox"))
    cube_vox(os.path.join(ws.art, "cube.vox"))
    package = ws.write_package("world.demo",
                               demo_world_ops(**(ops_kwargs or {})))
    manifest_path = ws.write_manifest(cells or demo_manifest_cells(), [package])
    return build_from_file(manifest_path, out_dir=ws.out, write=write)


class TestVoxReader(unittest.TestCase):

    def test_axes_are_converted_to_y_up(self):
        with BuildWorkspace() as ws:
            path = write_vox_file(os.path.join(ws.art, "one.vox"),
                                  (3, 5, 7), [(1, 2, 3, 4)])
            model = read_vox(path)[0]
            self.assertEqual(model.size, (3, 7, 5),
                             "MagicaVoxel is Z-up; Lobster is Y-up")
            self.assertEqual(model.voxels, {(1, 3, 2): 4})

    def test_models_are_padded_to_a_legal_cube(self):
        with BuildWorkspace() as ws:
            path = write_vox_file(os.path.join(ws.art, "odd.vox"),
                                  (3, 5, 7), [(0, 0, 0, 2)])
            model = read_vox(path)[0]
            self.assertEqual(cube_side(model, 8), 8)
            structure = to_structure(model, "odd", chunk_size=8)
            self.assertEqual(structure.grid_size, 8)
            self.assertEqual(structure.chunk_count, 1)
            self.assertTrue(structure.is_solid(0, 0, 0))
            self.assertFalse(structure.is_solid(7, 7, 7))

    def test_a_file_that_is_not_vox_says_so(self):
        with BuildWorkspace() as ws:
            path = os.path.join(ws.art, "bogus.vox")
            with open(path, "wb") as f:
                f.write(b"not a vox file at all")
            with self.assertRaises(VoxError) as ctx:
                read_vox(path)
            self.assertIn("not a MagicaVoxel", str(ctx.exception))


class TestTerrainMesher(unittest.TestCase):
    """The terrain half of L2 - a separate mesher, doing a different job."""

    def test_a_flat_cell_collapses_to_one_quad(self):
        field = flat_column_field(32)
        terrain = mesh_terrain("cell-flat", field)
        # one merged top face plus four boundary cliffs
        self.assertEqual(terrain.mesh.triangle_count(), 10)
        self.assertEqual(terrain.collider.ground_height(5.5, 7.5), 1.0)

    def test_a_terrace_produces_a_step(self):
        heights = [[1 if z < 16 else 3 for z in range(32)] for _ in range(32)]
        materials = [[1] * 32 for _ in range(32)]
        field = ColumnField(32, tuple(tuple(r) for r in heights),
                            tuple(tuple(r) for r in materials))
        terrain = mesh_terrain("cell-terrace", field)
        self.assertEqual(terrain.collider.ground_height(5.0, 8.0), 1.0)
        self.assertEqual(terrain.collider.ground_height(5.0, 24.0), 3.0)
        self.assertGreater(terrain.mesh.triangle_count(), 10)

    def test_the_two_meshers_share_nothing(self):
        """L2 / 15.1, and DECISIONS.md D11 explains the duplication."""
        from lobster import structure_mesher
        from lobster.build import terrain_mesher
        for module in (structure_mesher, terrain_mesher):
            with open(module.__file__, encoding="utf-8") as handle:
                imports = [line.strip() for line in handle
                           if line.strip().startswith(("import ", "from "))]
            other = ("terrain_mesher" if module is structure_mesher
                     else "structure_mesher")
            self.assertEqual(
                [i for i in imports if other in i], [],
                "{0} imports {1}".format(module.__name__, other))


class TestNavmeshInference(unittest.TestCase):
    """Scope 10 v0.4 - inferred, never authored (15.7 calls the reverse a
    regression)."""

    def setUp(self):
        self.navmesh = Navmesh("cell-a", [
            NavPoly(0, ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)), 0.0)])

    def structure(self, position):
        return StructureVoxelData(
            "keep", 16, bytes([1]) * 16 ** 3,
            origin=Transform(position=position), chunk_size=8)

    def test_a_chunk_on_the_navmesh_is_flagged(self):
        table = infer_load_bearing(self.navmesh, self.structure((0.0, 0.0, 0.0)))
        self.assertTrue(table.inferred, "a structure standing on the navmesh "
                                        "must be inferred load-bearing")
        self.assertTrue(any(table.is_load_bearing(i) for i in range(8)))

    def test_a_chunk_far_from_the_navmesh_is_not(self):
        table = infer_load_bearing(self.navmesh,
                                   self.structure((60.0, 0.0, 60.0)))
        self.assertEqual(table.inferred, {})

    def test_an_author_may_override_and_it_is_reported(self):
        structure = self.structure((0.0, 0.0, 0.0))
        table = infer_load_bearing(self.navmesh, structure,
                                   overrides={0: False})
        self.assertFalse(table.is_load_bearing(0))
        self.assertEqual(table.polys_for(0), ())
        report = inference_report(table, structure)
        self.assertEqual(report["overrides"][0]["effect"],
                         "suppresses an inferred load-bearing chunk")

    def test_a_dead_override_is_a_finding(self):
        structure = self.structure((0.0, 0.0, 0.0))
        table = infer_load_bearing(self.navmesh, structure,
                                   overrides={999: True})
        findings = validate_overrides(table, structure)
        self.assertEqual([f["code"] for f in findings],
                         ["override_out_of_range"])
        self.assertIn("override_out_of_range", ERROR_CODES)

    def test_empty_chunks_are_skipped(self):
        hollow = StructureVoxelData("hollow", 16, bytes(16 ** 3),
                                    origin=Transform(), chunk_size=8)
        table = infer_load_bearing(self.navmesh, hollow)
        self.assertEqual(table.inferred, {},
                         "an empty chunk cannot support or block anything")


class TestNavmeshBake(unittest.TestCase):

    def test_a_chasm_bakes_two_disconnected_polys(self):
        heights = [[0 if z in (7, 8) else 2 for z in range(16)]
                   for _ in range(16)]
        materials = [[1] * 16 for _ in range(16)]
        field = ColumnField(16, tuple(tuple(r) for r in heights),
                            tuple(tuple(r) for r in materials))
        navmesh = bake_navmesh("cell-split", field)
        self.assertEqual(len(navmesh.polys), 2)
        start = navmesh.poly_at((2.0, 2.0, 2.0))
        goal = navmesh.poly_at((2.0, 2.0, 14.0))
        self.assertIsNone(navmesh.find_path(start, goal),
                          "nothing bridges the chasm")

    def test_a_portal_lands_on_the_poly_under_the_spawn_point(self):
        navmesh = bake_navmesh("cell-a", flat_column_field(16),
                               portals={"cell-b": (2.0, 1.0, 2.0)})
        targets = [p.connection_target for p in navmesh.polys.values()]
        self.assertIn("cell-b", targets)

    def test_a_portal_off_the_navmesh_is_a_finding(self):
        navmesh = bake_navmesh("cell-a", flat_column_field(16))
        findings = unreachable_portals(navmesh, {"cell-b": (2.0, 0.0, 200.0)})
        self.assertEqual([f["code"] for f in findings], ["portal_off_navmesh"])


class TestBuildLint(unittest.TestCase):
    """Scope 15.2: "the build step should say so loudly"."""

    def lint_for(self, **ops_kwargs):
        ws = BuildWorkspace()
        self.addCleanup(ws.close)
        slab_vox(os.path.join(ws.art, "slab.vox"))
        cube_vox(os.path.join(ws.art, "cube.vox"))
        package = ws.write_package("world.demo", demo_world_ops(**ops_kwargs))
        manifest_path = ws.write_manifest(demo_manifest_cells(), [package])
        manifest = load_manifest(manifest_path)
        view = content_view(manifest.package_paths())
        return lint_world(view, manifest)

    def test_a_clean_world_lints_clean(self):
        self.assertEqual(errors(self.lint_for()), [])

    def test_a_one_way_connection_is_an_error(self):
        codes = [f["code"] for f in errors(self.lint_for(one_way=True))]
        self.assertIn("one_way_connection", codes)

    def test_an_unenterable_location_is_an_error(self):
        findings = errors(self.lint_for(one_way=True, no_spawn=True))
        unenterable = [f for f in findings
                       if f["code"] == "unenterable_location"]
        self.assertEqual([f["record_id"] for f in unenterable], ["cell-b"])
        self.assertIn("fast travel", unenterable[0]["detail"])

    def test_a_frozen_zone_shape_is_enforced(self):
        findings = errors(self.lint_for(bad_zone_shape=True))
        shapes = [f for f in findings if f["code"] == "unknown_zone_shape"]
        self.assertEqual([f["record_id"] for f in shapes], ["zone-bad"])
        self.assertIn("frozen primitives", shapes[0]["detail"])

    def test_a_structure_in_the_wrong_cell_is_an_error(self):
        findings = errors(self.lint_for(structure_location="cell-b"))
        codes = {f["code"] for f in findings}
        self.assertIn("structure_location_mismatch", codes)
        self.assertIn("structure_without_geometry", codes)

    def test_a_smuggled_sound_list_is_rejected(self):
        """Scope 4.5 - the build step must not special-case it into a bypass
        channel."""
        ws = BuildWorkspace()
        self.addCleanup(ws.close)
        slab_vox(os.path.join(ws.art, "slab.vox"))
        cube_vox(os.path.join(ws.art, "cube.vox"))
        package = ws.write_package("world.demo", demo_world_ops())
        cells = demo_manifest_cells(
            sound_sources=[{"position": [1, 0, 1], "sound_id": "snd-forge",
                            "radius": 8}])
        manifest_path = ws.write_manifest(cells, [package])
        manifest = load_manifest(manifest_path)
        view = content_view(manifest.package_paths())
        findings = errors(lint_world(view, manifest))
        bypass = [f for f in findings if f["code"] == "sound_bypass_channel"]
        self.assertEqual(len(bypass), 1)
        self.assertEqual(bypass[0]["cell_id"], "cell-a")
        self.assertIn("ordinary layered content", bypass[0]["detail"])

    def test_a_failing_lint_bakes_nothing(self):
        with BuildWorkspace() as ws:
            report = build_demo(ws, ops_kwargs={"one_way": True})
            self.assertFalse(report.ok())
            self.assertEqual(report.written, [])
            self.assertFalse(os.path.isdir(ws.out)
                             and os.listdir(ws.out))


class TestBuildRoundTrip(unittest.TestCase):

    def test_build_then_load(self):
        with BuildWorkspace() as ws:
            report = build_demo(ws)
            self.assertTrue(report.ok(), report.errors())
            self.assertEqual(len(report.written), 2)

            bundle = read_bundle(os.path.join(ws.out, "cell-a.lobster_cell"))
            self.assertEqual(bundle.cell_id, "cell-a")
            self.assertEqual(bundle.structure_ids(), ["gatehouse"])
            self.assertEqual(bundle.structure("gatehouse").origin.position,
                             (6.0, 2.0, 6.0))
            self.assertTrue(bundle.table_for("gatehouse").inferred,
                            "the gatehouse stands on the navmesh")
            self.assertIn("cell-a", bundle.provenance["records"])
            self.assertIn("gatehouse", bundle.provenance["records"])

            # and the runtime can make it resident
            package = os.path.join(ws.path, "world.demo.json")
            view = content_view(list(load_manifest(
                os.path.join(ws.path, "world.manifest.json")).package_paths()))
            manager = CellManager(ws.out)
            cell = manager.load(view, "cell-a")
            self.assertEqual(sorted(cell.structures), ["gatehouse"])
            self.assertEqual(cell.structure("gatehouse").destroyed, set())
            self.assertIsNotNone(cell.navmesh)
            self.assertGreater(cell.terrain.mesh.triangle_count(), 0)

    def test_a_dry_run_writes_nothing(self):
        with BuildWorkspace() as ws:
            report = build_demo(ws, write=False)
            self.assertTrue(report.ok())
            self.assertEqual(report.written, [])
            self.assertTrue(report.cells)

    def test_the_bundle_is_a_pure_function_of_its_inputs(self):
        """DECISIONS.md D1 - a bundle holds no state, so two builds match."""
        with BuildWorkspace() as ws:
            build_demo(ws)
            with open(os.path.join(ws.out, "cell-a.lobster_cell"), "rb") as f:
                first = f.read()
        with BuildWorkspace() as ws2:
            build_demo(ws2)
            with open(os.path.join(ws2.out, "cell-a.lobster_cell"), "rb") as f:
                second = f.read()
        self.assertEqual(first, second,
                         "the same inputs must bake to the same bytes")

    def test_a_duplicate_cell_is_refused(self):
        with BuildWorkspace() as ws:
            slab_vox(os.path.join(ws.art, "slab.vox"))
            cube_vox(os.path.join(ws.art, "cube.vox"))
            package = ws.write_package("world.demo", demo_world_ops())
            cells = demo_manifest_cells() + [{"location_id": "cell-a",
                                              "terrain_vox": "slab.vox"}]
            path = ws.write_manifest(cells, [package])
            with self.assertRaises(BuildError) as ctx:
                build_from_file(path, out_dir=ws.out)
            self.assertIn("more than once", str(ctx.exception))


class TestLighting(unittest.TestCase):

    def test_a_pit_is_darker_than_a_ridge(self):
        heights = [[6] * 16 for _ in range(16)]
        for x in range(6, 10):
            for z in range(6, 10):
                heights[x][z] = 1
        materials = [[1] * 16 for _ in range(16)]
        field = ColumnField(16, tuple(tuple(r) for r in heights),
                            tuple(tuple(r) for r in materials))
        lightmap = bake_lightmap("cell-pit", field)
        self.assertLess(lightmap.level(8, 8), lightmap.level(0, 0))

    def test_a_structure_samples_ambient_at_its_position(self):
        """Scope 3: no per-structure light grid to store or update on damage."""
        lightmap = bake_lightmap("cell-flat", flat_column_field(16))
        self.assertGreater(lightmap.ambient_at((4.0, 2.0, 4.0)), 0.0)
        self.assertLessEqual(lightmap.ambient_at((4.0, 2.0, 4.0)), 1.0)


if __name__ == "__main__":
    unittest.main()


class TestItemPlacementLint(unittest.TestCase):
    """Scope 8: `world_transform` and `current_location_ref` are co-null
    (CONTRACT §2, DECISIONS.md D33).

    An earlier draft of this checked only one direction and asserted that a
    location with no transform was clean - "it must be an inventory item".
    Octopus says otherwise: `StdLib.location_of` maps an Item to
    `current_location_ref`, so a location *is* the claim that the thing is out
    in the world. Half-placed in either direction, it silently never appears.
    """

    class FakeView:
        def __init__(self, items):
            self.items = items

        def records_of_type(self, type_name):
            return list(self.items) if type_name == "Item" else []

    def check(self, **fields):
        item = {"id": "item-sword", "type": "Item"}
        item.update(fields)
        return check_item_placements(self.FakeView([item]))

    def test_a_transform_with_no_cell_is_an_error(self):
        found = self.check(world_transform={"position": [1.0, 0.0, 2.0]})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "item_transform_without_location")
        self.assertEqual(found[0]["record_id"], "item-sword")
        self.assertIn(found[0], errors(found), "this must fail a build")

    def test_a_cell_with_no_transform_is_an_error(self):
        found = self.check(current_location_ref="cell-village")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "item_location_without_transform")
        self.assertEqual(found[0]["cell_id"], "cell-village",
                         "the finding must name the cell it would have been in")
        self.assertIn(found[0], errors(found))

    def test_it_names_the_octopus_restriction_behind_the_common_cause(self):
        """An author shipping a pre-placed sword hits Octopus's
        save_layer_only on current_location_ref. The finding says so rather
        than leaving them to work it out."""
        found = self.check(world_transform={"position": [1.0, 0.0, 2.0]})
        self.assertIn("save_layer_only", found[0]["detail"])
        self.assertIn("place_item", found[0]["detail"])

    def test_both_halves_present_is_clean(self):
        self.assertEqual(self.check(world_transform={"position": [1, 0, 2]},
                                    current_location_ref="cell-village"), [])

    def test_neither_half_present_is_clean(self):
        """Not being in the world is the normal state of most items."""
        self.assertEqual(self.check(), [])

    def test_both_codes_fail_a_build(self):
        self.assertIn("item_transform_without_location", ERROR_CODES)
        self.assertIn("item_location_without_transform", ERROR_CODES)
