"""Camera, visibility and the software rasteriser (Scope 1, 3).

Shrimp's observation was that Lobster shipped every input to a renderer and no
renderer: `TerrainMesh`, `StructureMesher`, `bone_matrices` and the baked
lightmap were all produced and none was read. These tests are the consumer.

Two of them exist because rendering found bugs that numbers alone did not:

* the terrain mesher wound its top surface so the normal pointed **down**,
  which no geometry test noticed and which would break lighting and backface
  culling in any renderer;
* dropping triangles that cross the near plane silently discarded the ground,
  because terrain is greedy-meshed into a few enormous quads and the camera
  stands on one of them.

See DECISIONS.md D21.
"""

from __future__ import annotations

import math
import os
import unittest

from lobster.build.terrain_mesher import (ColumnField, flat_column_field,
                                          mesh_terrain)
from lobster.camera import Camera, CameraError
from lobster.geometry import AABB, Transform, cross, normalize, sub
from lobster.render import (RenderSettings, SoftwareBackend, render_cell,
                            write_png)
from lobster.render.png import encode_png
from lobster.render.raster import (Framebuffer, ITEM_COLOUR,
                                   PROP_COLOUR, _clip_near,
                                   render_draw_list)
from lobster.cell import CellManager
from lobster.octopus_bridge import OctopusBridge
from lobster.skeleton import Skeleton, humanoid_region_set
from tests.fixtures import VILLAGE, build_session, standard_workspace
from lobster.tiers import ACTIVE, DORMANT
from lobster.visibility import (DRAW_KINDS, ITEM, ITEM_DRAW_RADIUS_M,
                                DrawList, ENTITY, STRUCTURE, TERRAIN,
                                build_draw_list)


class TestCamera(unittest.TestCase):

    def setUp(self):
        self.camera = Camera.looking_at((0.0, 2.0, 0.0), (0.0, 2.0, 10.0),
                                        aspect=16.0 / 9.0)

    def test_the_basis_is_right_handed_and_orthonormal(self):
        right, up, forward = self.camera.basis()
        for a, b in ((right, up), (right, forward), (up, forward)):
            self.assertAlmostEqual(sum(x * y for x, y in zip(a, b)), 0.0,
                                   places=9)
        derived = cross(right, up)
        for got, want in zip(derived, forward):
            self.assertAlmostEqual(got, want, places=9,
                                   msg="right x up must equal forward, or the "
                                       "image comes out mirrored")

    def test_screen_axes_point_the_expected_way(self):
        centre = self.camera.project((0, 2, 10), 640, 360)
        right = self.camera.project((3, 2, 10), 640, 360)
        above = self.camera.project((0, 5, 10), 640, 360)
        self.assertAlmostEqual(centre[0], 320.0, places=6)
        self.assertAlmostEqual(centre[1], 180.0, places=6)
        self.assertGreater(right[0], centre[0], "world +x must be screen-right")
        self.assertLess(above[1], centre[1], "world +y must be screen-up")

    def test_a_point_behind_the_camera_does_not_project(self):
        self.assertIsNone(self.camera.project((0, 2, -5), 640, 360))

    def test_culling_errs_outward(self):
        self.assertTrue(self.camera.sees_sphere((0, 2, 50), 1.0))
        self.assertFalse(self.camera.sees_sphere((0, 2, -50), 1.0))
        self.assertFalse(self.camera.sees_sphere((500, 2, 50), 1.0))
        self.assertTrue(self.camera.sees_sphere((30, 2, 50), 8.0),
                        "a sphere straddling the frustum edge counts as visible")

    def test_far_things_are_culled(self):
        self.assertFalse(self.camera.sees_aabb(AABB((-5, 0, 900), (5, 10, 910))))

    def test_a_degenerate_camera_is_refused(self):
        with self.assertRaises(CameraError):
            Camera.looking_at((0, 0, 0), (0, 0, 0))
        with self.assertRaises(CameraError):
            Camera(near=1.0, far=0.5)


class TestTerrainNormals(unittest.TestCase):
    """The bug the first render found."""

    def normals(self, terrain):
        verts, indices = terrain.mesh.vertices, terrain.mesh.indices
        out = []
        for i in range(0, len(indices), 3):
            tri = [(verts[indices[i + k] * 3], verts[indices[i + k] * 3 + 1],
                    verts[indices[i + k] * 3 + 2]) for k in range(3)]
            out.append(tuple(round(c, 6) for c in
                             normalize(cross(sub(tri[1], tri[0]),
                                             sub(tri[2], tri[0])))))
        return out

    def test_the_ground_faces_up(self):
        normals = self.normals(mesh_terrain("c", flat_column_field(8)))
        self.assertIn((0.0, 1.0, 0.0), normals)
        self.assertNotIn((0.0, -1.0, 0.0), normals,
                         "a downward-facing ground shades as if lit from below "
                         "and is discarded by backface culling")

    def test_every_cliff_faces_outward(self):
        heights = [[1 if z < 4 else 3 for z in range(8)] for _ in range(8)]
        materials = [[1] * 8 for _ in range(8)]
        field = ColumnField(8, tuple(tuple(r) for r in heights),
                            tuple(tuple(r) for r in materials))
        normals = set(self.normals(mesh_terrain("t", field)))
        self.assertNotIn((0.0, -1.0, 0.0), normals)
        for axis in ((1.0, 0.0, 0.0), (-1.0, 0.0, 0.0),
                     (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)):
            self.assertIn(axis, normals,
                          "cliffs on all four sides must point away from the "
                          "hill they belong to")


class TestNearPlaneClipping(unittest.TestCase):
    """The other bug the first render found."""

    def test_a_triangle_straddling_the_near_plane_survives(self):
        clipped = _clip_near([(0.0, 0.0, 5.0), (0.0, 1.0, -5.0),
                              (1.0, 0.0, 5.0)], 0.1)
        self.assertGreaterEqual(len(clipped), 3)
        self.assertTrue(all(p[2] >= 0.1 - 1e-9 for p in clipped),
                        "nothing may survive the clip on the wrong side")

    def test_a_triangle_entirely_behind_is_removed(self):
        self.assertEqual(_clip_near([(0.0, 0.0, -1.0), (1.0, 0.0, -2.0),
                                     (0.0, 1.0, -3.0)], 0.1), [])

    def test_a_triangle_entirely_in_front_is_untouched(self):
        points = [(0.0, 0.0, 5.0), (1.0, 0.0, 5.0), (0.0, 1.0, 5.0)]
        self.assertEqual(_clip_near(points, 0.1), points)

    def test_the_ground_draws_when_the_camera_stands_on_it(self):
        """The whole point: a huge quad with a corner behind you still fills
        the screen."""
        cell = _FakeCell(mesh_terrain("c", flat_column_field(24)))
        camera = Camera.looking_at((12.0, 3.0, 12.0), (20.0, 1.0, 20.0))
        frame = render_cell(camera, cell, backend=SoftwareBackend(),
                            settings=RenderSettings(width=160, height=90))
        self.assertGreater(frame.coverage(), 0.25,
                           "the ground was dropped for crossing the near plane")


class _FakeCell:
    """The smallest thing `render_cell` accepts - terrain and nothing else."""

    def __init__(self, terrain):
        self.cell_id = "cell-fake"
        self.terrain = terrain
        self.structures = {}
        self.skeletons = {}
        self.index = None
        self.bundle = None


class TestVisibility(unittest.TestCase):

    def setUp(self):
        self.cell = _FakeCell(mesh_terrain("cell-fake", flat_column_field(24)))

    def test_only_what_the_camera_sees_is_in_the_draw_list(self):
        looking_at_it = Camera.looking_at((12.0, 6.0, 0.0), (12.0, 1.0, 12.0))
        # Stand well clear before turning away: the culling bound is a sphere
        # around the whole 24 m patch, so from inside it "facing the other way"
        # is legitimately still visible.
        away = Camera.looking_at((12.0, 6.0, -100.0), (12.0, 6.0, -400.0))
        self.assertEqual(len(build_draw_list(looking_at_it, [self.cell]).items), 1)
        self.assertEqual(build_draw_list(away, [self.cell]).items, ())

    def test_stats_report_what_culling_saved(self):
        away = Camera.looking_at((12.0, 6.0, -100.0), (12.0, 6.0, -400.0))
        stats = build_draw_list(away, [self.cell]).stats
        self.assertEqual(stats.items_considered, 1)
        self.assertEqual(stats.items_drawn, 0)
        self.assertEqual(stats.culled(), 1)
        self.assertEqual(stats.cells_drawn, 0)

    def test_dormant_entities_are_not_drawn(self):
        """A DORMANT entity has no rig by definition (CONTRACT §3), so asking
        to draw one would be asking for something that does not exist."""
        from lobster.spatial import SpatialIndex
        index = SpatialIndex("cell-fake")
        index.snapshot([("mob", (12.0, 1.0, 12.0), DORMANT),
                        ("knight", (13.0, 1.0, 12.0), ACTIVE)])
        self.cell.index = index
        self.cell.skeletons = {"knight": Skeleton(
            "knight", humanoid_region_set(),
            root=Transform(position=(13.0, 1.0, 12.0)))}
        camera = Camera.looking_at((12.0, 4.0, 4.0), (13.0, 1.0, 12.0))
        entities = build_draw_list(camera, [self.cell]).of_kind(ENTITY)
        self.assertEqual([i.item_id for i in entities], ["knight"])

    def test_items_come_back_nearest_first(self):
        from lobster.spatial import SpatialIndex
        index = SpatialIndex("cell-fake")
        index.snapshot([("near", (12.0, 1.0, 8.0), ACTIVE),
                        ("far", (12.0, 1.0, 20.0), ACTIVE)])
        self.cell.index = index
        self.cell.skeletons = {
            name: Skeleton(name, humanoid_region_set(),
                           root=Transform(position=(12.0, 1.0, z)))
            for name, z in (("near", 8.0), ("far", 20.0))}
        camera = Camera.looking_at((12.0, 3.0, 0.0), (12.0, 1.0, 20.0))
        items = build_draw_list(camera, [self.cell]).items
        distances = [i.distance for i in items]
        self.assertEqual(distances, sorted(distances))

    def test_a_cell_with_no_placement_sits_at_the_origin(self):
        """Lobster does not invent where cells are (DECISIONS.md D21)."""
        camera = Camera.looking_at((12.0, 6.0, 0.0), (12.0, 1.0, 12.0))
        item = build_draw_list(camera, [self.cell]).items[0]
        self.assertEqual(item.cell_placement.position, (0.0, 0.0, 0.0))

    def test_a_placement_moves_a_cell(self):
        camera = Camera.looking_at((12.0, 6.0, 0.0), (12.0, 1.0, 12.0))
        far = build_draw_list(
            camera, [self.cell],
            placements={"cell-fake": Transform(position=(0.0, 0.0, 600.0))})
        self.assertEqual(far.items, (),
                         "moved 600 m away it is past the far plane, bounding "
                         "sphere included")


class TestRasteriser(unittest.TestCase):

    def test_it_actually_draws(self):
        cell = _FakeCell(mesh_terrain("cell-fake", flat_column_field(24)))
        camera = Camera.looking_at((12.0, 4.0, 2.0), (12.0, 1.0, 18.0))
        frame = render_cell(camera, cell, backend=SoftwareBackend(),
                            settings=RenderSettings(width=160, height=90))
        self.assertGreater(frame.pixels_written, 0)
        self.assertGreater(frame.coverage(), 0.1)

    def test_an_empty_view_leaves_the_background(self):
        cell = _FakeCell(mesh_terrain("cell-fake", flat_column_field(24)))
        settings = RenderSettings(width=64, height=36)
        camera = Camera.looking_at((12.0, 6.0, 0.0), (12.0, 20.0, -200.0))
        frame = render_cell(camera, cell, settings=settings,
                            backend=SoftwareBackend())
        self.assertEqual(frame.coverage(), 0.0)
        self.assertEqual(frame.pixel(0, 0), settings.background)

    def test_the_z_buffer_keeps_the_nearer_surface(self):
        frame = Framebuffer(4, 4, (0, 0, 0))
        self.assertTrue(frame.put(1, 1, 10.0, (255, 0, 0)))
        self.assertFalse(frame.put(1, 1, 20.0, (0, 255, 0)),
                         "a farther fragment must not overwrite a nearer one")
        self.assertEqual(frame.pixel(1, 1), (255, 0, 0))
        self.assertTrue(frame.put(1, 1, 5.0, (0, 0, 255)))
        self.assertEqual(frame.pixel(1, 1), (0, 0, 255))

    def test_a_pose_is_what_gets_drawn(self):
        """`bone_matrices`/`capsule_for` finally have a consumer."""
        from lobster.spatial import SpatialIndex
        cell = _FakeCell(mesh_terrain("cell-fake", flat_column_field(24)))
        index = SpatialIndex("cell-fake")
        index.snapshot([("knight", (12.0, 1.0, 14.0), ACTIVE)])
        cell.index = index
        skeleton = Skeleton("knight", humanoid_region_set(),
                            root=Transform(position=(12.0, 1.0, 14.0)))
        cell.skeletons = {"knight": skeleton}
        camera = Camera.looking_at((12.0, 2.0, 10.0), (12.0, 1.8, 14.0))
        settings = RenderSettings(width=120, height=90, draw_entities=True)

        with_entity = render_cell(camera, cell, settings=settings,
                                  backend=SoftwareBackend()).pixels_written
        without = render_cell(
            camera, cell, backend=SoftwareBackend(),
            settings=RenderSettings(width=120, height=90,
                                    draw_entities=False)).pixels_written
        self.assertGreater(with_entity, without,
                           "drawing the rig must put pixels on the screen")


class TestPng(unittest.TestCase):

    def test_it_writes_a_real_png(self):
        data = encode_png(2, 2, [255, 0, 0,  0, 255, 0,
                                 0, 0, 255,  255, 255, 255])
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data[:32])
        self.assertTrue(data.endswith(b"IEND\xae\x42\x60\x82"))

    def test_the_wrong_number_of_bytes_is_refused(self):
        with self.assertRaises(ValueError):
            encode_png(2, 2, [0, 0, 0])


if __name__ == "__main__":
    unittest.main()


class TestPlacedItemsReachTheDrawList(unittest.TestCase):
    """Scope §8 step 5 (DECISIONS.md D37).

    Items are not baked into the bundle, so unlike props they need the live
    view to appear at all. Without this a dropped sword would be pickable,
    labellable and invisible - which is exactly the class of silent
    disagreement §13 invariant 2 exists to prevent.
    """

    def setUp(self):
        self.session = build_session()
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-sword", "type": "Item", "display_name": "Sword"}})
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.manager = CellManager(self.ws.path, session=self.session)
        view = self.bridge.frame()
        self.manager.set_player_cell(view, VILLAGE)
        self.manager.place_item(view, "item-sword", VILLAGE,
                                Transform(position=(6.0, 0.5, 8.0)))
        self.view = self.bridge.frame()
        self.cells = [self.manager.resident[c]
                      for c in sorted(self.manager.resident)]

    def draw_list(self, **kwargs):
        camera = Camera.looking_at((6.0, 1.6, 0.0), (6.0, 1.0, 20.0))
        return build_draw_list(camera, self.cells,
                               placements=self.manager.placements(self.view),
                               **kwargs)

    def test_a_placed_item_is_drawn_when_a_view_is_given(self):
        drawn = [i for i in self.draw_list(view=self.view).items
                 if i.kind == ITEM]
        self.assertEqual([i.item_id for i in drawn], ["item-sword"])
        self.assertEqual(drawn[0].cell_id, VILLAGE)

    def test_without_a_view_there_are_no_items(self):
        """Right for a build-step preview, which has baked geometry and no
        save to read."""
        self.assertEqual([i for i in self.draw_list().items
                          if i.kind == ITEM], [])

    def test_a_removed_item_stops_being_drawn(self):
        self.manager.remove_item(self.bridge.frame(), "item-sword")
        self.view = self.bridge.frame()
        self.assertEqual([i for i in self.draw_list(view=self.view).items
                          if i.kind == ITEM], [])

    def test_the_draw_radius_errs_larger_than_the_pick_radius(self):
        """A cull must err towards drawing: culling something invisible costs
        one wasted draw, culling something visible is a missing sword."""
        from lobster.selection import ITEM_PICK_RADIUS_M
        self.assertGreater(ITEM_DRAW_RADIUS_M, ITEM_PICK_RADIUS_M)

    def test_item_is_a_declared_draw_kind(self):
        self.assertIn(ITEM, DRAW_KINDS)

    def test_render_resident_forwards_the_view(self):
        """Otherwise items would be pickable and invisible."""
        import inspect
        from lobster.render import raster
        source = inspect.getsource(raster.render_resident)
        self.assertIn("view=view", source)


class TestEveryDrawKindReachesPixels(unittest.TestCase):
    """The gap that scoping the GPU backend found (RENDER_SCOPE §0).

    `build_draw_list` culled items in and `render_draw_list` had no branch for
    them, so a placed item was pickable, labellable and **invisible** - which
    is the precise failure D37 congratulated itself on preventing, one layer
    below where that entry looked. The dispatch is now pinned against the
    declared kind set, so adding a sixth kind and forgetting to draw it fails
    here rather than in somebody's screenshot.
    """

    def setUp(self):
        self.session = build_session()
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-sword", "type": "Item", "display_name": "Sword"}})
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.manager = CellManager(self.ws.path, session=self.session)
        view = self.bridge.frame()
        self.manager.set_player_cell(view, VILLAGE)
        self.manager.place_item(view, "item-sword", VILLAGE,
                                Transform(position=(6.0, 0.4, 8.0)))
        self.view = self.bridge.frame()

    def frame_with(self, **kwargs):
        camera = Camera.looking_at((6.0, 1.2, 2.0), (6.0, 0.6, 12.0))
        cells = [self.manager.resident[c] for c in sorted(self.manager.resident)]
        settings = RenderSettings(width=160, height=90, **kwargs)
        draw_list = build_draw_list(camera.with_aspect(settings.aspect()),
                                    cells,
                                    placements=self.manager.placements(self.view),
                                    view=self.view)
        return draw_list, render_draw_list(
            draw_list, {c.cell_id: c for c in cells}, settings=settings)

    def test_every_kind_in_the_draw_list_has_a_branch(self):
        """The structural half: no kind may be silently dropped."""
        import inspect
        from lobster.render import raster
        source = inspect.getsource(raster.render_draw_list)
        for kind_name in ("TERRAIN", "STRUCTURE", "ENTITY", "PROP", "ITEM"):
            self.assertIn("item.kind == " + kind_name, source,
                          "{0} reaches the draw list with no way to be "
                          "drawn".format(kind_name))

    def test_a_placed_item_actually_changes_pixels(self):
        """The behavioural half. A branch that exists and paints nothing would
        satisfy the test above and still be the same bug.

        Measured by rendering the same scene with the item and without and
        diffing the buffers. An earlier version counted pixels "near" the item
        colour, which with any usable tolerance also counted the terrain -
        6,146 of 14,400 - and said nothing. A diff needs no tolerance and does
        not care about the sun angle.
        """
        draw_list, lit = self.frame_with()
        self.assertIn(ITEM, [i.kind for i in draw_list.items],
                      "fixture must put an item in the draw list")

        self.manager.remove_item(self.bridge.frame(), "item-sword")
        self.view = self.bridge.frame()
        _, without = self.frame_with()

        changed = _differing_pixels(lit, without)
        self.assertGreater(changed, 0,
                           "the item is in the draw list and on no pixel")
        self.assertLess(changed, lit.width * lit.height // 4,
                        "a sword should not repaint a quarter of the screen - "
                        "{0} pixels changed, which means something other than "
                        "the item moved".format(changed))

    def test_the_item_is_drawn_where_it_was_placed(self):
        """Not merely somewhere. The changed pixels must sit near where the
        camera projects the item's own position."""
        draw_list, lit = self.frame_with()
        entry = next(i for i in draw_list.items if i.kind == ITEM)
        projected = draw_list.camera.project(entry.center, lit.width, lit.height)
        self.assertIsNotNone(projected, "fixture must have the item on screen")

        self.manager.remove_item(self.bridge.frame(), "item-sword")
        self.view = self.bridge.frame()
        _, without = self.frame_with()

        xs, ys = _changed_bounds(lit, without)
        self.assertLessEqual(abs((xs[0] + xs[1]) / 2 - projected[0]), 12.0)
        self.assertLessEqual(abs((ys[0] + ys[1]) / 2 - projected[1]), 12.0)

    def test_items_are_not_drawn_as_props(self):
        """A viewer that cannot tell them apart cannot tell you whether the
        sword on the floor is a real one."""
        self.assertNotEqual(ITEM_COLOUR, PROP_COLOUR)


def _differing_pixels(a, b):
    return sum(1 for y in range(a.height) for x in range(a.width)
               if a.pixel(x, y) != b.pixel(x, y))


def _changed_bounds(a, b):
    xs = [x for y in range(a.height) for x in range(a.width)
          if a.pixel(x, y) != b.pixel(x, y)]
    ys = [y for y in range(a.height) for x in range(a.width)
          if a.pixel(x, y) != b.pixel(x, y)]
    return (min(xs), max(xs)), (min(ys), max(ys))
