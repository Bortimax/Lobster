"""The third seam kernel, wired (DECISIONS.md D53).

D40 and D42 built kernels behind the seam and measured them, and nothing called
them - every entry for several after had to say so. So the tests that matter
most here are not about the kernel. They are about whether it is *used*, and
whether using it changed any answer:

1. **The same draw list, on every implementation.** Culling and packing moved
   into C; if the picture moved with them, the kernel is not a faster path, it
   is a different one.
2. **The backend uploads the bytes the culler already packed**, rather than
   packing them again - which is the whole reason the kernel returns them.
3. **The fallback still works**, because a draw list built by hand has no
   packed bytes and must still draw.

The kernel's own correctness is the differential harness's job
(`test_conformance.py`), where it is compared against the reference over
thousands of generated cases.
"""

from __future__ import annotations

import math
import unittest

from lobster import accel
from lobster.budgets import Budget
from lobster.bundle import PropPlacement
from lobster.camera import Camera
from lobster.cell import ResidentCell
from lobster.conformance import INSTANCE_FLOATS, PLACE_BATCH, PLACEMENT_FLOATS
from lobster.geometry import Transform
from lobster.render import RenderSettings
from lobster.render.gl_backend import INSTANCE_STRIDE, ModernGLBackend
from lobster.render.recording import RecordingContext
from lobster.visibility import ITEM, PROP, build_draw_list
from tests.fixtures import plain_bundle, primitive_library

CELL = "cell-k"
BARREL = "model-barrel"
CRATE = "model-crate"


def props(count, model_ref=BARREL, spin=False):
    out = []
    for i in range(count):
        # from i + 1, so the first prop is turned too - `i * 7`
        # made prop 0 an identity rotation and a test that asked
        # for a rotation got none.
        half = math.radians((i + 1) * 7.0) if spin else 0.0
        out.append(PropPlacement(
            prop_id="p%d" % i, model_ref=model_ref,
            transform=Transform(
                position=(2.0 + (i % 20) * 0.4, 0.0, 6.0 + (i // 20) * 0.4),
                rotation=(0.0, math.sin(half), 0.0, math.cos(half)))))
    return out


class KernelFixture(unittest.TestCase):

    def setUp(self):
        self.library = primitive_library()
        self.settings = RenderSettings(width=160, height=90)
        self.camera = Camera.looking_at(
            (6.0, 4.0, -8.0), (6.0, 0.5, 10.0)).with_aspect(
                self.settings.aspect())

    def cell(self, placements):
        return ResidentCell(plain_bundle(CELL, props=placements),
                            Budget.declared(CELL, {}))

    def draw(self, placements, **kwargs):
        return build_draw_list(self.camera, [self.cell(placements)],
                               library=self.library,
                               max_visible_placements=0, **kwargs)

    def using(self, implementation):
        """Force one implementation for the length of a `with` block."""
        import contextlib

        @contextlib.contextmanager
        def swap():
            original = accel.KERNEL_PREFERENCE
            accel.KERNEL_PREFERENCE = dict(
                original, **{PLACE_BATCH: (implementation,)})
            try:
                yield
            finally:
                accel.KERNEL_PREFERENCE = original
        return swap()

    def implementations(self):
        out = ["python"]
        for entry in accel.available():
            if entry["available"] and entry["name"] != "python":
                out.append(entry["name"])
        return out


# ---------------------------------------------------------------------------
# 1. The answer did not move
# ---------------------------------------------------------------------------

def _implementations():
    return [e["name"] for e in accel.available() if e["available"]]


@unittest.skipIf(len(_implementations()) < 2,
                 "only the reference is available here, so there is nothing "
                 "to compare it against - which is exactly the configuration "
                 "the pure-python CI job exists to prove works")
class TestEveryImplementationDrawsTheSameList(KernelFixture):

    @staticmethod
    def digest(draw_list):
        return [(i.kind, i.item_id, i.model_ref,
                 tuple(round(c, 6) for c in i.center), round(i.radius, 6),
                 round(i.distance, 6),
                 tuple(round(c, 6) for c in i.transform.position)
                 if i.transform else None,
                 tuple(round(c, 6) for c in i.transform.rotation)
                 if i.transform else None)
                for i in draw_list.items]

    def test_the_draw_list_is_identical_across_implementations(self):
        placements = props(120, spin=True)
        baseline = None
        for name in self.implementations():
            with self.subTest(implementation=name):
                with self.using(name):
                    got = self.digest(self.draw(placements))
                if baseline is None:
                    baseline = got
                    self.assertTrue(baseline, "nothing was drawn at all")
                else:
                    self.assertEqual(got, baseline,
                                     "{0} draws a different list".format(name))

    def test_the_packed_instances_are_identical_across_implementations(self):
        placements = props(120, spin=True)
        baseline = None
        for name in self.implementations():
            with self.subTest(implementation=name):
                with self.using(name):
                    blobs = {k: bytes(v)
                             for k, v in self.draw(placements).instances.items()}
                if baseline is None:
                    baseline = blobs
                    self.assertTrue(baseline)
                else:
                    self.assertEqual(sorted(blobs), sorted(baseline))
                    for key in blobs:
                        # f32 either side of f64 maths, so exact bytes are not
                        # promised - the declared tolerance is what is.
                        self.assertEqual(len(blobs[key]), len(baseline[key]))

    def test_more_than_one_implementation_was_actually_compared(self):
        """Otherwise the two tests above are a very slow way of comparing
        Python to itself, which is the failure `conformance` calls out.

        A guard on the class above rather than a failure on its own: a machine
        with no compiler and no numpy is a **supported** configuration - L7
        promises it and a whole CI job asserts it - so "there is nothing to
        compare" is an honest skip there and a real fault anywhere else. The
        first version of this failed that job.
        """
        self.assertGreater(len(_implementations()), 1)


# ---------------------------------------------------------------------------
# 2. The rows that go in
# ---------------------------------------------------------------------------

class TestThePlacementRows(KernelFixture):

    def test_they_are_grouped_by_model(self):
        cell = self.cell(props(3, BARREL) + props(2, CRATE))
        rows = cell.prop_rows()
        self.assertEqual(sorted(rows), [BARREL, CRATE])
        self.assertEqual(len(rows[BARREL][0]), 3 * PLACEMENT_FLOATS)
        self.assertEqual(len(rows[CRATE][1]), 2)

    def test_they_are_built_once_per_residency(self):
        """A prop does not move, so rebuilding these every frame would be
        per-placement Python work in front of a kernel that exists to remove
        per-placement Python work."""
        cell = self.cell(props(4))
        self.assertIs(cell.prop_rows(), cell.prop_rows())

    def test_a_prop_with_no_model_is_its_own_group(self):
        cell = self.cell(props(2, BARREL) + props(1, ""))
        self.assertIn("", cell.prop_rows())
        self.assertEqual(len(cell.prop_rows()[""][1]), 1)

    def test_the_rows_carry_the_rotation(self):
        cell = self.cell(props(1, BARREL, spin=True))
        flat, _ids = cell.prop_rows()[BARREL]
        self.assertEqual(len(flat), PLACEMENT_FLOATS)
        self.assertNotEqual(tuple(flat[3:7]), (0.0, 0.0, 0.0, 1.0))

    def test_the_lightmap_block_matches_the_cell_reader(self):
        """One bake, two readers, and a test that says they agree - because
        two readers of one format is how they drift."""
        import dataclasses
        import random
        from lobster.conformance import sample_lightmap
        rng = random.Random(9)
        side = 8
        data = bytes(rng.randrange(256) for _ in range(side * side))
        bundle = dataclasses.replace(plain_bundle(CELL), lightmap=data,
                                     lightmap_dims=(side, 1, side),
                                     lightmap_voxel_size=1.0)
        cell = ResidentCell(bundle, Budget.declared(CELL, {}))
        block = cell.lightmap_block()
        for _ in range(100):
            point = (rng.uniform(-15, 25), 0.0, rng.uniform(-15, 25))
            self.assertAlmostEqual(sample_lightmap(block, point),
                                   cell.ambient_at(point), places=9)

    def test_an_unlit_cell_has_no_block(self):
        self.assertIsNone(self.cell(props(1)).lightmap_block())


class TestThePayloadTheKernelIsGiven(KernelFixture):
    """The bridge between the runtime and the kernel, asserted directly.

    Five mutations survived everything else in this file: wrong camera fields,
    a dropped cell placement, a dropped lightmap, a wrong voxel size, dropped
    item rotations. All five were invisible because the *software* path
    recomputes from `DrawItem.cell_placement` and `DrawItem.transform` and
    never reads what the kernel returned - so only culling and the packed
    instances depend on the payload, and neither was being varied.
    """

    def payload(self, placement=None, lightmap=None):
        from lobster.visibility import _place_payload
        return _place_payload(self.camera, placement or Transform(), 1.0,
                              [0.0] * PLACEMENT_FLOATS, lightmap)

    def test_it_carries_the_camera_s_fields_not_a_derived_basis(self):
        """A payload carrying the basis would hide the one derivation most
        likely to differ between implementations."""
        cam = self.payload()["camera"]
        self.assertEqual(cam["position"], self.camera.position)
        self.assertEqual(cam["forward"], self.camera.forward)
        self.assertEqual(cam["up"], self.camera.up)
        self.assertEqual(cam["fov_y_deg"], self.camera.fov_y_deg)
        self.assertEqual(cam["aspect"], self.camera.aspect)
        self.assertEqual(cam["near"], self.camera.near)
        self.assertEqual(cam["far"], self.camera.far)
        self.assertNotIn("right", cam)

    def test_it_carries_the_cell_placement(self):
        placement = Transform(position=(3.0, 1.0, -2.0),
                              rotation=(0.0, 0.3826834, 0.0, 0.9238795))
        block = self.payload(placement)["cell"]
        self.assertEqual(tuple(block["position"]), placement.position)
        self.assertEqual(tuple(block["rotation"]), placement.rotation)

    def test_it_carries_the_lightmap(self):
        block = {"data": b"@" * 64, "side": 8, "voxel_size": 2.0}
        self.assertIs(self.payload(lightmap=block)["lightmap"], block)

    def test_a_narrower_field_of_view_culls_more(self):
        """The proof that `fov_y_deg` reaches the kernel rather than a
        hard-coded default.

        Spread over 120 m on purpose: the standard fixture puts every prop in
        an 8 m huddle 15 m away, which even a 15-degree cone sees all of - so
        the first version of this test compared 60 against 60.
        """
        placements = [PropPlacement(
            prop_id="w%d" % i, model_ref=BARREL,
            transform=Transform(position=(-60.0 + i * 2.0, 0.0, 20.0)))
            for i in range(60)]

        def seen(fov):
            return len(build_draw_list(
                Camera.looking_at((0.0, 2.0, 0.0), (0.0, 1.0, 20.0),
                                  fov_y_deg=fov).with_aspect(2.0),
                [self.cell(placements)], library=self.library,
                max_visible_placements=0).of_kind(PROP))

        wide, narrow = seen(110.0), seen(15.0)
        self.assertGreater(wide, narrow,
                           "a 15-degree cone drew as much as a 110-degree one")
        self.assertGreater(narrow, 0, "the narrow camera sees nothing at all")


class TestWhatThePackedInstanceActuallyHolds(KernelFixture):
    """Unpacked and checked, because the packed bytes are the only thing on
    the GPU path that the software path cannot vouch for."""

    def instance(self, placements, placement=None, cell=None):
        import struct
        target = cell or self.cell(placements)
        draw_list = build_draw_list(
            self.camera, [target], library=self.library,
            placements={CELL: placement} if placement else None,
            max_visible_placements=0)
        blob = draw_list.instances[(CELL, BARREL)]
        items = [i for i in draw_list.items if i.model_ref == BARREL]
        return struct.unpack_from("%df" % INSTANCE_FLOATS, blob, 0), items[0]

    def test_the_translation_is_the_cell_placement_composed_in(self):
        placement = Transform(position=(4.0, 1.0, -3.0))
        floats, item = self.instance(props(1), placement=placement)
        expected = placement.compose(item.transform).position
        for slot, want in zip((12, 13, 14), expected):
            self.assertAlmostEqual(floats[slot], want, places=4)
        self.assertNotAlmostEqual(floats[12], item.transform.position[0],
                                  places=3,
                                  msg="the cell placement made no difference")

    def test_the_centre_agrees_with_the_translation(self):
        placement = Transform(position=(4.0, 1.0, -3.0))
        floats, item = self.instance(props(1), placement=placement)
        for slot, want in zip((12, 13, 14), item.center):
            self.assertAlmostEqual(floats[slot], want, places=4)

    def test_a_lit_cell_sends_its_light(self):
        import dataclasses
        bundle = dataclasses.replace(plain_bundle(CELL, props=props(1)),
                                     lightmap=bytes([64]) * 64,
                                     lightmap_dims=(8, 1, 8),
                                     lightmap_voxel_size=1.0)
        cell = ResidentCell(bundle, Budget.declared(CELL, {}))
        floats, _item = self.instance(None, cell=cell)
        self.assertAlmostEqual(floats[16], 64 / 255.0, places=5)

    def test_the_voxel_size_reaches_the_sampler(self):
        """Two cells with the same bytes and different voxel sizes light the
        same prop differently - which is what makes the number load-bearing."""
        import dataclasses
        data = bytes(range(64))

        def light_at(voxel_size):
            bundle = dataclasses.replace(
                plain_bundle(CELL, props=props(1)), lightmap=data,
                lightmap_dims=(8, 1, 8), lightmap_voxel_size=voxel_size)
            cell = ResidentCell(bundle, Budget.declared(CELL, {}))
            return self.instance(None, cell=cell)[0][16]

        self.assertNotAlmostEqual(light_at(1.0), light_at(4.0), places=4)


# ---------------------------------------------------------------------------
# 3. What comes out, and who uses it
# ---------------------------------------------------------------------------

class TestTheDrawListCarriesThePackedInstances(KernelFixture):

    def test_one_blob_per_cell_and_model(self):
        draw_list = self.draw(props(6, BARREL) + props(4, CRATE))
        self.assertEqual(sorted(draw_list.instances),
                         [(CELL, BARREL), (CELL, CRATE)])

    def test_each_blob_is_one_instance_per_visible_placement(self):
        draw_list = self.draw(props(30))
        drawn = [i for i in draw_list.items
                 if i.kind == PROP and i.model_ref == BARREL]
        self.assertTrue(drawn)
        self.assertEqual(len(draw_list.instances[(CELL, BARREL)]),
                         len(drawn) * INSTANCE_FLOATS * 4)

    def test_a_prop_with_no_model_gets_no_blob(self):
        draw_list = self.draw(props(3, ""))
        self.assertEqual(draw_list.instances, {})
        self.assertTrue(draw_list.of_kind(PROP), "nothing was drawn at all")

    def test_a_draw_list_with_no_library_carries_none(self):
        draw_list = build_draw_list(self.camera, [self.cell(props(5))],
                                    max_visible_placements=0)
        self.assertEqual(draw_list.instances, {})

    def test_the_blob_is_not_in_to_dict(self):
        """It is a buffer, not a description, and a draw list dump is read by
        people."""
        self.assertNotIn("instances", self.draw(props(2)).to_dict())


class TestTheBackendUsesThemRatherThanRepacking(KernelFixture):

    def backend(self, placements, **kwargs):
        cell = self.cell(placements)
        ctx = RecordingContext()
        backend = ModernGLBackend(ctx, width=self.settings.width,
                                  height=self.settings.height)
        backend.upload_cell(cell)
        for ref in self.library.model_refs():
            backend.upload_model(self.library.model(ref))
        draw_list = build_draw_list(self.camera, [cell], library=self.library,
                                    max_visible_placements=0, **kwargs)
        backend.render(draw_list, {CELL: cell}, self.settings,
                       library=self.library)
        return backend, draw_list, cell, ctx

    def test_nothing_is_packed_twice(self):
        """The culler has the world transform and the light in registers at the
        moment it culls; handing them back would mean computing them twice."""
        backend, _dl, _cell, _ctx = self.backend(props(40))
        self.assertEqual(backend._repacked, 0)

    def test_the_uploaded_bytes_are_the_culler_s_bytes(self):
        backend, draw_list, cell, ctx = self.backend(props(25))
        blob = draw_list.instances[(CELL, BARREL)]
        writes = [c for c in ctx.of("buffer.write")
                  if c.detail.get("nbytes") == len(blob)]
        self.assertTrue(writes,
                        "no upload matched the culler's blob of {0} bytes"
                        .format(len(blob)))

    def test_a_hand_built_draw_list_still_draws(self):
        """The fallback. A caller that culled for itself, or a fixture, has no
        packed bytes and must not silently draw nothing."""
        from dataclasses import replace
        cell = self.cell(props(12))
        ctx = RecordingContext()
        backend = ModernGLBackend(ctx, width=self.settings.width,
                                  height=self.settings.height)
        backend.upload_cell(cell)
        backend.upload_model(self.library.model(BARREL))
        draw_list = build_draw_list(self.camera, [cell], library=self.library,
                                    max_visible_placements=0)
        stripped = replace(draw_list, instances={})
        backend.render(stripped, {CELL: cell}, self.settings)
        self.assertGreater(backend._repacked, 0, "the fallback never ran")
        draws = [c for c in ctx.of("render")
                 if (c.detail.get("instances") or -1) > 0]
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0].detail["instances"],
                         len(draw_list.instances[(CELL, BARREL)])
                         // INSTANCE_STRIDE)

    def test_a_blob_that_does_not_match_is_refused_rather_than_uploaded(self):
        """The length check is a guard, not an optimisation: a stream that does
        not line up with the items it is drawn against would put every barrel
        at the wrong barrel's transform."""
        from dataclasses import replace
        cell = self.cell(props(12))
        ctx = RecordingContext()
        backend = ModernGLBackend(ctx, width=self.settings.width,
                                  height=self.settings.height)
        backend.upload_cell(cell)
        backend.upload_model(self.library.model(BARREL))
        draw_list = build_draw_list(self.camera, [cell], library=self.library,
                                    max_visible_placements=0)
        short = dict(draw_list.instances)
        short[(CELL, BARREL)] = short[(CELL, BARREL)][:-INSTANCE_STRIDE]
        backend.render(replace(draw_list, instances=short), {CELL: cell},
                       self.settings)
        self.assertGreater(backend._repacked, 0,
                           "a short blob was uploaded as though it fitted")


# ---------------------------------------------------------------------------
# Items go through it too
# ---------------------------------------------------------------------------

class TestPlacedItemsUseTheKernel(unittest.TestCase):

    def setUp(self):
        from lobster.cell import CellManager
        from lobster.events import EventBus
        from lobster.octopus_bridge import OctopusBridge
        from tests.fixtures import (BundleWorkspace, VILLAGE, build_session,
                                    village_bundle)
        self.VILLAGE = VILLAGE
        self.session = build_session()
        self.session.engine.write({"op": "CREATE", "record": {
            "id": CRATE, "type": "Model"}})
        for i in range(6):
            self.session.engine.write({"op": "CREATE", "record": {
                "id": "item-%d" % i, "type": "Item",
                "display_name": "Thing %d" % i, "model_ref": CRATE}})
        self.bridge = OctopusBridge(self.session)
        self.ws = BundleWorkspace()
        self.addCleanup(self.ws.close)
        self.ws.write(village_bundle())
        self.ws.write_library(primitive_library())
        self.manager = CellManager(self.ws.path, bus=EventBus(),
                                   session=self.session)
        view = self.bridge.frame()
        self.manager.load(view, VILLAGE)
        for i in range(6):
            self.manager.place_item(self.bridge.frame(), "item-%d" % i,
                                    VILLAGE,
                                    Transform(position=(5.0 + i * 0.5, 0.0,
                                                        8.0)))
        self.view = self.bridge.frame()
        self.settings = RenderSettings(width=160, height=90)
        self.camera = Camera.looking_at(
            (6.0, 3.0, 2.0), (6.0, 0.5, 9.0)).with_aspect(
                self.settings.aspect())

    def test_items_are_culled_and_packed_by_the_kernel(self):
        cell = self.manager.resident[self.VILLAGE]
        draw_list = build_draw_list(self.camera, [cell],
                                    library=primitive_library(),
                                    view=self.view,
                                    max_visible_placements=0)
        drawn = [i for i in draw_list.items if i.kind == ITEM]
        self.assertTrue(drawn, "no item was drawn at all")
        self.assertEqual(len(draw_list.instances[(self.VILLAGE, CRATE)]),
                         len(drawn) * INSTANCE_FLOATS * 4)

    def test_an_item_keeps_its_rotation(self):
        """Every item in the fixture above is placed square, so dropping the
        rotation from the item rows changed nothing and survived every other
        test here. A dropped sword lands at an angle."""
        half = math.radians(50.0 / 2.0)
        spun = Transform(position=(6.0, 0.0, 9.0),
                         rotation=(0.0, math.sin(half), 0.0, math.cos(half)))
        self.manager.place_item(self.bridge.frame(), "item-0", self.VILLAGE,
                                spun)
        view = self.bridge.frame()
        cell = self.manager.resident[self.VILLAGE]
        draw_list = build_draw_list(self.camera, [cell],
                                    library=primitive_library(), view=view,
                                    max_visible_placements=0)
        drawn = {i.item_id: i for i in draw_list.items if i.kind == ITEM}
        self.assertIn("item-0", drawn)
        for got, want in zip(drawn["item-0"].transform.rotation, spun.rotation):
            self.assertAlmostEqual(got, want, places=6)

    def test_an_item_keeps_its_own_transform_and_identity(self):
        cell = self.manager.resident[self.VILLAGE]
        draw_list = build_draw_list(self.camera, [cell],
                                    library=primitive_library(),
                                    view=self.view,
                                    max_visible_placements=0)
        by_id = {i.item_id: i for i in draw_list.items if i.kind == ITEM}
        self.assertIn("item-3", by_id)
        self.assertAlmostEqual(by_id["item-3"].transform.position[0], 6.5,
                               places=6)


if __name__ == "__main__":
    unittest.main()
