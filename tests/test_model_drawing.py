"""Props and items drawn as real geometry, software first (ASSET_SCOPE §7 step 4).

The scope called this step unskippable and said why:

> It is the only place a wrong mesh is **visibly wrong** rather than merely
> *different from the GPU*. RENDER_SCOPE §2 deleted pixel agreement between the
> backends, which was right and which also means the GPU path has no oracle: a
> mesh drawn inside-out, at the wrong scale, or with inverted normals would
> render, differ from the software path, and be indistinguishable from the
> legitimate differences that decision permits.

So these tests are mostly about *pixels*, and about the three ways a mesh can be
put in the wrong place: the object's own transform, the cell's placement, and
the anchor the library baked in (D47). A test that only counted triangles would
pass for a model drawn a hundred metres away.
"""

from __future__ import annotations

import math
import unittest

from lobster.budgets import Budget
from lobster.bundle import PropPlacement
from lobster.camera import Camera
from lobster.cell import ResidentCell
from lobster.geometry import Transform
from lobster.model_library import ModelLibrary
from lobster.render import RenderSettings
from lobster.render.raster import render_cell, render_draw_list
from lobster.render.software_backend import SoftwareBackend
from lobster.visibility import ITEM, PROP, build_draw_list
from tests.fixtures import plain_bundle, primitive_library, prop

CELL = "cell-look"
CRATE = "model-crate"
BARREL = "model-barrel"
SIGN = "model-sign"


def placed(prop_id, model_ref, position, degrees=0.0):
    half = math.radians(degrees / 2.0)
    return PropPlacement(
        prop_id=prop_id, model_ref=model_ref,
        transform=Transform(position=position,
                            rotation=(0.0, math.sin(half), 0.0,
                                      math.cos(half))))


def same_frame(a, b):
    """Whether two framebuffers are pixel-identical.

    A plain `assertEqual` on the two colour lists would say the same thing and
    would build a unified diff of a hundred thousand bytes when it failed -
    which is exactly when it runs. unittest's differ is superlinear, and a
    mutation run that should have taken three minutes sat for ten before this
    helper existed.
    """
    return bytes(a.colour) == bytes(b.colour)


class Pixels:
    """What is actually on the screen, so a test can say where a crate is."""

    def __init__(self, frame, settings):
        self.frame = frame
        self.background = tuple(settings.background)
        self.width, self.height = frame.width, frame.height

    def at(self, x, y):
        i = (y * self.width + x) * 3
        return tuple(self.frame.colour[i:i + 3])

    def drawn(self, baseline):
        """Which pixels this prop put on the screen, against the same scene
        without it.

        Defined as a *difference* and not as "not the background", which was
        this helper's first draft: the ground is a huge single colour and the
        obvious heuristic was to call the commonest non-sky colour the ground -
        which also swallowed the crate whenever the terrain moved with it. A
        mutation that dropped the cell placement from `_draw_model` passed
        cleanly under that version.
        """
        return [(x, y) for y in range(self.height) for x in range(self.width)
                if self.at(x, y) != baseline.at(x, y)]

    def centroid(self, baseline):
        points = self.drawn(baseline)
        if not points:
            return None
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))


class DrawingFixture(unittest.TestCase):

    WIDTH, HEIGHT = 240, 135

    def settings(self):
        return RenderSettings(width=self.WIDTH, height=self.HEIGHT)

    def cell(self, props):
        return ResidentCell(plain_bundle(CELL, props=props),
                            Budget.declared(CELL, {}))

    def camera(self, eye=(6.0, 1.6, 7.6), target=(6.0, 0.4, 10.0)):
        settings = self.settings()
        return Camera.looking_at(eye, target).with_aspect(settings.aspect())

    def render(self, props, *, library=..., **camera_kwargs):
        library = primitive_library() if library is ... else library
        settings = self.settings()
        frame = render_cell(self.camera(**camera_kwargs), self.cell(props),
                            settings=settings, library=library,
                            backend=SoftwareBackend())
        return Pixels(frame, settings)

    def empty(self, **camera_kwargs):
        """The same scene with no props at all - the baseline every positional
        assertion is a difference against."""
        return self.render([], **camera_kwargs)


# ---------------------------------------------------------------------------
# A mesh, and not an impostor
# ---------------------------------------------------------------------------

class TestRealGeometryReachesThePixels(DrawingFixture):

    def test_a_prop_with_a_model_does_not_look_like_one_without(self):
        with_model = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))])
        without = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))],
                              library=None)
        self.assertFalse(same_frame(with_model.frame, without.frame),
                         "the library made no difference to the picture, so "
                         "nothing is drawing the mesh")
        self.assertTrue(with_model.drawn(self.empty()),
                        "nothing was drawn at all")

    def test_the_three_shapes_do_not_draw_the_same_picture(self):
        """A box, a cylinder and a quad at the same spot. If any two frames
        matched, one generator would be producing another's geometry."""
        frames = [bytes(self.render([placed("p", ref, (6.0, 0.0, 10.0))])
                        .frame.colour)
                  for ref in (CRATE, BARREL, SIGN)]
        self.assertEqual(len(set(frames)), 3,
                         "two of the three shapes drew the same picture")

    def test_a_prop_with_no_model_is_still_the_impostor(self):
        """Not a placeholder to be removed - it is what a thing with no mesh
        looks like (ASSET_SCOPE §4)."""
        with_library = self.render([placed("n", "", (6.0, 0.0, 10.0))])
        without = self.render([placed("n", "", (6.0, 0.0, 10.0))],
                              library=None)
        self.assertTrue(same_frame(with_library.frame, without.frame),
                        "a prop with no model drew something other than the "
                        "impostor")
        self.assertTrue(with_library.drawn(self.empty()))

    def test_a_model_ref_the_library_lacks_falls_back_and_does_not_raise(self):
        """A build error (`prop_model_ref_unresolved`) and a counted residency
        fault (`missing_models`). A renderer that threw mid-frame over one
        absent barrel would take the whole picture with it."""
        ghost = self.render([placed("g", "model-ghost", (6.0, 0.0, 10.0))])
        impostor = self.render([placed("g", "model-ghost", (6.0, 0.0, 10.0))],
                               library=None)
        self.assertTrue(same_frame(ghost.frame, impostor.frame),
                        "an unresolvable model_ref did not fall back to the "
                        "impostor")

    def test_the_model_s_material_reaches_the_pixels(self):
        """Same shape, two materials, two colours.

        The three-shapes test above cannot see this: a crate and a barrel share
        material 6, so replacing every tint with white left them still telling
        each other apart by geometry, and that mutation survived.
        """
        from lobster.build.model_mesher import mesh_primitive
        from lobster.render.raster import DEFAULT_PALETTE

        def box_of(material):
            ref = "model-box-%d" % material
            library = ModelLibrary(models={ref: mesh_primitive(
                ref, {"shape": "box", "size": [0.8, 0.8, 0.8],
                      "material": material})})
            pixels = self.render([placed("b", ref, (6.0, 0.0, 10.0))],
                                 library=library)
            return pixels, pixels.drawn(self.empty())

        timber, timber_pixels = box_of(6)
        grass, grass_pixels = box_of(2)
        self.assertTrue(timber_pixels and grass_pixels)
        self.assertTrue(timber_pixels == grass_pixels,
                        "the two boxes are not the same shape in the same "
                        "place, so a colour difference proves nothing")
        self.assertNotEqual({timber.at(x, y) for x, y in timber_pixels},
                            {grass.at(x, y) for x, y in grass_pixels})
        # and not white, which is what an ignored tint looks like
        self.assertNotIn((255, 255, 255),
                         {timber.at(x, y) for x, y in timber_pixels})
        # The hues follow the palette. Compared as red-minus-green and not as
        # "timber is redder", which is false: grass (122, 148, 92) has more red
        # than timber (110, 86, 60) and less of it relative to green.
        def warmth(pixels, points):
            return sum(pixels.at(x, y)[0] - pixels.at(x, y)[1]
                       for x, y in points) / len(points)

        self.assertGreater(warmth(timber, timber_pixels), 0.0)
        self.assertLess(warmth(grass, grass_pixels), 0.0)
        self.assertGreater(DEFAULT_PALETTE[6][0] - DEFAULT_PALETTE[6][1],
                           DEFAULT_PALETTE[2][0] - DEFAULT_PALETTE[2][1],
                           "the fixture cannot tell the two materials apart")

    def test_a_model_in_the_library_with_no_triangles_draws_the_impostor(self):
        """The build refuses to write one (`model_meshes_to_nothing`), so this
        is a library and a bundle that were built apart. Falling back is right;
        handing zero vertices to the rasteriser and drawing nothing at all
        would be a hole in the picture with nobody to blame."""
        from lobster.model_library import ModelMesh
        hollow = ModelLibrary(models={CRATE: ModelMesh(model_ref=CRATE,
                                                       kind="voxel")})
        empty_model = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))],
                                  library=hollow)
        impostor = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))],
                               library=None)
        self.assertTrue(same_frame(empty_model.frame, impostor.frame),
                        "a model with no triangles did not fall back to the "
                        "impostor")
        self.assertTrue(empty_model.drawn(self.empty()),
                        "nothing was drawn at all - the fallback did not fire")

    def test_an_empty_library_draws_the_impostor(self):
        empty = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))],
                            library=ModelLibrary())
        none = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))],
                           library=None)
        self.assertTrue(same_frame(empty.frame, none.frame),
                        "an empty library drew something other than the "
                        "impostor")


# ---------------------------------------------------------------------------
# The three ways to put a mesh in the wrong place
# ---------------------------------------------------------------------------

class TestWhereTheMeshLands(DrawingFixture):

    def test_moving_a_prop_moves_its_pixels(self):
        empty = self.empty()
        left = self.render([placed("c", CRATE, (5.0, 0.0, 10.0))]).centroid(empty)
        right = self.render([placed("c", CRATE, (7.0, 0.0, 10.0))]).centroid(empty)
        self.assertIsNotNone(left)
        self.assertLess(left[0], right[0],
                        "the prop's own transform is not being applied - both "
                        "crates drew in the same place")

    def test_rotating_a_prop_turns_it_without_moving_it(self):
        """D47's anchoring decision, in pixels.

        A corner-anchored model turned 45 degrees swings around its corner and
        ends up somewhere else. Centred on X and Z, it turns in place: the
        picture changes, the centre does not.
        """
        straight = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))])
        turned = self.render([placed("c", CRATE, (6.0, 0.0, 10.0), 45.0)])
        self.assertFalse(same_frame(straight.frame, turned.frame),
                         "rotation changed nothing, so it is not applied")
        empty = self.empty()
        a, b = straight.centroid(empty), turned.centroid(empty)
        self.assertAlmostEqual(a[0], b[0], delta=2.0,
                               msg="the crate swung sideways when it was turned "
                                   "- it is anchored at a corner, not centred")
        self.assertAlmostEqual(a[1], b[1], delta=2.0)

    def test_a_model_sits_on_the_ground_it_is_placed_on(self):
        """Anchored at y = 0, so a prop at ground level does not float or sink.

        Checked as "the crate's lowest pixel is below the horizon and its
        highest is above the ground it stands on" rather than by an exact row,
        which would be a test of the projection maths.
        """
        empty = self.empty()
        pixels = self.render([placed("c", CRATE, (6.0, 0.0, 10.0))])
        rows = [y for _x, y in pixels.drawn(empty)]
        floating = self.render([PropPlacement(
            prop_id="c", model_ref=CRATE,
            transform=Transform(position=(6.0, 1.0, 10.0)))])
        high = [y for _x, y in floating.drawn(empty)]
        self.assertLess(min(high), min(rows),
                        "lifting the prop a metre did not raise it on screen")

    def test_the_cell_placement_is_applied_too(self):
        """Two transforms, in order: the prop inside its cell, then the cell in
        the world. Missing the second draws every neighbour on top of the cell
        you are standing in."""
        settings = self.settings()
        library = primitive_library()
        offset = {CELL: Transform(position=(3.0, 0.0, 0.0))}

        def shot(props, placements):
            cell = self.cell(props)
            draw_list = build_draw_list(self.camera(), [cell], library=library,
                                        placements=placements)
            return Pixels(render_draw_list(draw_list, {CELL: cell},
                                           settings=settings, library=library),
                          settings)

        crate = [placed("c", CRATE, (6.0, 0.0, 10.0))]
        # Each shot is compared against *its own* empty scene, so the terrain
        # moving with the cell cannot stand in for the crate moving with it.
        a = shot(crate, None).centroid(shot([], None))
        b = shot(crate, offset).centroid(shot([], offset))
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertGreater(b[0], a[0] + 5.0,
                           "the crate did not move with its cell - the cell "
                           "placement is not reaching the model")


# ---------------------------------------------------------------------------
# Lighting, which the library deliberately does not carry
# ---------------------------------------------------------------------------

class TestLighting(DrawingFixture):

    class Dim:
        """A cell whose baked light is a fraction of full."""

        def __init__(self, inner, level):
            self.inner = inner
            self.level = level

        def ambient_at(self, point):
            return self.level

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def frame_at(self, level, props=None):
        settings = self.settings()
        if props is None:
            props = [placed("c", CRATE, (6.0, 0.0, 10.0))]
        cell = self.Dim(self.cell(props), level)
        library = primitive_library()
        draw_list = build_draw_list(self.camera(), [cell], library=library)
        return Pixels(render_draw_list(draw_list, {CELL: cell},
                                       settings=settings, library=library),
                      settings)

    def test_the_cell_s_baked_light_multiplies_the_model_s_tint(self):
        """The library tint is unlit on purpose (D47): a mesh shared by many
        cells cannot carry one cell's bake, so the cell supplies it here."""
        bright = self.frame_at(1.0)
        dark = self.frame_at(0.2)

        empty = self.frame_at(1.0, props=[])

        def brightest(pixels):
            return max(sum(pixels.at(x, y)) for x, y in pixels.drawn(empty))

        self.assertGreater(brightest(bright), brightest(dark),
                           "a dim cell drew the crate exactly as bright as a "
                           "lit one - the lightmap is not reaching the model")


# ---------------------------------------------------------------------------
# Culling, which now knows how big a thing is
# ---------------------------------------------------------------------------

class TestTheCullRadiusComesFromTheModel(DrawingFixture):

    def draw_items(self, props, library):
        return build_draw_list(self.camera(), [self.cell(props)],
                               library=library).of_kind(PROP)

    def test_a_draw_item_carries_its_model_and_its_transform(self):
        items = self.draw_items([placed("c", CRATE, (6.0, 0.0, 10.0))],
                                primitive_library())
        self.assertEqual([i.model_ref for i in items], [CRATE])
        self.assertEqual(items[0].transform.position, (6.0, 0.0, 10.0))
        self.assertEqual(items[0].to_dict()["model_ref"], CRATE)

    def test_the_radius_is_the_model_s_when_there_is_one(self):
        library = primitive_library()
        radius = self.draw_items([placed("b", BARREL, (6.0, 0.0, 10.0))],
                                 library)[0].radius
        self.assertAlmostEqual(
            radius, library.model(BARREL).bound_radius(), places=6)

    def test_a_prop_with_no_model_keeps_the_invented_guess(self):
        items = self.draw_items([placed("n", "", (6.0, 0.0, 10.0))],
                                primitive_library())
        self.assertEqual(items[0].radius, 1.0)
        self.assertEqual(items[0].model_ref, "")

    def test_a_model_bigger_than_the_guess_is_not_culled_by_it(self):
        """A cull must err towards drawing. A statue taller than the invented
        1 m would pop out of view while still on screen."""
        from lobster.build.model_mesher import mesh_primitive
        tall = ModelLibrary(models={"model-statue": mesh_primitive(
            "model-statue", {"shape": "box", "size": [0.4, 6.0, 0.4],
                             "material": 1})})
        radius = self.draw_items([placed("s", "model-statue", (6.0, 0.0, 10.0))],
                                 tall)[0].radius
        self.assertGreater(radius, 3.0)

    def test_the_radius_never_shrinks_below_the_guess(self):
        """A model smaller than the guess keeps the guess. Erring towards
        drawing means the bound may be generous and must not be tight."""
        items = self.draw_items([placed("s", SIGN, (6.0, 0.0, 10.0))],
                                primitive_library())
        self.assertGreaterEqual(items[0].radius, 1.0)


# ---------------------------------------------------------------------------
# Items, which are records rather than bundle contents
# ---------------------------------------------------------------------------

class TestPlacedItemsDrawTheirModelToo(unittest.TestCase):

    WIDTH, HEIGHT = 240, 135

    def setUp(self):
        from lobster.cell import CellManager
        from lobster.events import EventBus
        from lobster.octopus_bridge import OctopusBridge
        from tests.fixtures import (BundleWorkspace, VILLAGE, build_session,
                                    village_bundle)
        self.VILLAGE = VILLAGE
        self.session = build_session()
        self.session.engine.write({"op": "CREATE", "record": {
            "id": "item-sword", "type": "Item", "display_name": "Sword",
            "model_ref": CRATE}})
        self.session.engine.write({"op": "CREATE", "record": {
            "id": CRATE, "type": "Model"}})
        self.bridge = OctopusBridge(self.session)
        self.ws = BundleWorkspace()
        self.addCleanup(self.ws.close)
        self.ws.write(village_bundle())
        self.ws.write_library(primitive_library())
        self.manager = CellManager(self.ws.path, bus=EventBus(),
                                   session=self.session)
        view = self.bridge.frame()
        self.manager.load(view, VILLAGE)
        self.manager.place_item(view, "item-sword", VILLAGE,
                                Transform(position=(6.0, 0.0, 8.0)))
        self.view = self.bridge.frame()

    def items(self, library):
        settings = RenderSettings(width=self.WIDTH, height=self.HEIGHT)
        camera = Camera.looking_at((6.0, 1.6, 5.0),
                                   (6.0, 0.4, 8.0)).with_aspect(
                                       settings.aspect())
        cell = self.manager.resident[self.VILLAGE]
        return build_draw_list(camera, [cell], library=library,
                               view=self.view).of_kind(ITEM), cell, settings

    def test_an_item_carries_its_model_ref_and_world_transform(self):
        items, _cell, _settings = self.items(primitive_library())
        self.assertEqual([i.model_ref for i in items], [CRATE])
        self.assertEqual(items[0].transform.position, (6.0, 0.0, 8.0))

    def test_it_draws_as_geometry_rather_than_an_impostor(self):
        library = primitive_library()
        _items, cell, settings = self.items(library)
        camera = Camera.looking_at((6.0, 1.6, 5.0),
                                   (6.0, 0.4, 8.0)).with_aspect(
                                       settings.aspect())
        with_model = render_draw_list(
            build_draw_list(camera, [cell], library=library, view=self.view),
            {self.VILLAGE: cell}, settings=settings, library=library)
        without = render_draw_list(
            build_draw_list(camera, [cell], view=self.view),
            {self.VILLAGE: cell}, settings=settings)
        self.assertFalse(same_frame(with_model, without),
                         "the item drew the same with and without a library, "
                         "so it is still an impostor")

    def test_the_library_reaches_the_renderer_through_the_manager(self):
        """`render_resident` reads it off the manager, which owns the bundle
        directory (D48). Nothing in the render path opens a file.

        Asserted as a *difference*: an earlier version only checked that some
        pixels were written, which stayed true with the library never passed on
        at all.
        """
        from lobster.render.raster import render_resident
        camera = Camera.looking_at((6.0, 1.6, 5.0), (6.0, 0.4, 8.0))
        settings = RenderSettings(width=self.WIDTH, height=self.HEIGHT)
        with_library = render_resident(camera, self.manager, self.view,
                                       settings=settings,
                                       backend=SoftwareBackend())
        self.manager._library = ModelLibrary()
        without = render_resident(camera, self.manager, self.view,
                                  settings=settings,
                                  backend=SoftwareBackend())
        self.assertGreater(with_library.pixels_written, 0)
        self.assertFalse(same_frame(with_library, without),
                         "emptying the manager's library changed nothing, so "
                         "`render_resident` is not passing it on")

    def test_the_culler_gets_it_too_and_not_only_the_rasteriser(self):
        """Two consumers, one artifact, and the pixels can only see one of them.

        `render_resident` hands the library to `build_draw_list` *and* to the
        backend. Dropping the first leaves every model still drawn - only the
        cull radii revert to the invented guesses - so a frame comparison
        cannot tell. That mutation survived until this test existed.
        """
        from lobster.render.raster import render_resident
        from lobster.render.recording import RecordingBackend
        library = primitive_library()
        recorder = RecordingBackend()
        camera = Camera.looking_at((6.0, 1.6, 5.0), (6.0, 0.4, 8.0))
        render_resident(camera, self.manager, self.view,
                        settings=RenderSettings(width=self.WIDTH,
                                                height=self.HEIGHT),
                        backend=recorder)
        self.assertEqual(recorder.libraries[-1].model_refs(),
                         library.model_refs(),
                         "the backend was handed no library")
        items = [i for i in recorder.draw_lists[-1].items if i.kind == ITEM]
        self.assertEqual([i.model_ref for i in items], [CRATE])
        self.assertAlmostEqual(items[0].radius,
                               library.model(CRATE).bound_radius(), places=6,
                               msg="the item was culled against the invented "
                                   "guess, so the culler never saw the library")


if __name__ == "__main__":
    unittest.main()
