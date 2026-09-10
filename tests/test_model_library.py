"""The shared model library (ASSET_SCOPE §2, step 2 of §7).

Two kinds go in and one vertex format comes out. The tests are grouped by the
claims the scope made, because those are the things that can be wrong:

1. **Primitives resolve with no file at all.** The reason they were added, and
   the reason the scope said to build them first: they prove the library end to
   end before an importer is involved.
2. **A voxel model goes through the mesher that already exists**, and comes out
   in the same format a primitive does.
3. **Anchoring is taken from the meshed extent**, so `.vox` padding cannot move
   a model.
4. **The palette is resolved at build time**, from the file's own table.
5. **Fifty placements are one mesh.** The whole justification for a library
   rather than baking a barrel into every cell that shows one.
6. **It is derived** (D1): reproducible byte-for-byte, and safe to delete.
"""

from __future__ import annotations

import os
import struct
import unittest

from lobster.build.builder import build_from_file
from lobster.build.library_writer import write_library
from lobster.build.manifest import load_manifest
from lobster.build.model_mesher import (anchor, mesh_primitive,
                                        mesh_voxel_file, primitive_triangles,
                                        tint_of)
from lobster.constants import (CYLINDER_SEGMENTS, LIBRARY_FILENAME,
                               LIBRARY_FORMAT, MODEL_PRIMITIVE,
                               MODEL_VERTEX_STRIDE, MODEL_VOXEL,
                               PRIMITIVE_SHAPES)
from lobster.model_library import (LIBRARY_MAGIC, ModelLibrary,
                                   ModelLibraryError, read_library,
                                   read_library_header)
from lobster.render.raster import DEFAULT_PALETTE
from tests.fixtures import (BuildWorkspace, C, cube_vox, demo_manifest_cells,
                            demo_world_ops, slab_vox, write_vox_file)

CRATE = {"shape": "box", "size": [0.8, 0.8, 0.8], "material": 6}
BARREL = {"shape": "cylinder", "radius": 0.35, "height": 0.9, "material": 6}
SIGN = {"shape": "quad", "size": [0.6, 0.4], "material": 5}


def vertices_of(mesh):
    """Unpack a packed model back into (position, normal, tint) triples."""
    count = mesh.vertex_count()
    out = []
    for i in range(count):
        values = struct.unpack_from("9f", mesh.vertices, i * MODEL_VERTEX_STRIDE)
        out.append((values[0:3], values[3:6], values[6:9]))
    return out


# ---------------------------------------------------------------------------
# 1. Primitives, which need no file
# ---------------------------------------------------------------------------

class TestPrimitivesMeshWithNoAssetAtAll(unittest.TestCase):

    def test_a_box_is_twelve_triangles(self):
        mesh = mesh_primitive("model-crate", CRATE)
        self.assertEqual(mesh.kind, MODEL_PRIMITIVE)
        self.assertEqual(mesh.triangle_count(), 12)
        self.assertEqual(mesh.nbytes(), 36 * MODEL_VERTEX_STRIDE)

    def test_a_cylinder_is_sides_plus_two_caps(self):
        mesh = mesh_primitive("model-barrel", BARREL)
        self.assertEqual(mesh.triangle_count(), CYLINDER_SEGMENTS * 4,
                         "two triangles a side and one a cap, twice")

    def test_a_quad_is_two_triangles(self):
        self.assertEqual(mesh_primitive("model-sign", SIGN).triangle_count(), 2)

    def test_every_frozen_shape_meshes(self):
        """A fourth shape that nobody taught the generator would show up here
        as a KeyError rather than as a model that silently does not appear."""
        for spec in (CRATE, BARREL, SIGN):
            self.assertIn(spec["shape"], PRIMITIVE_SHAPES)
            self.assertTrue(primitive_triangles(spec))
        self.assertEqual(sorted({s["shape"] for s in (CRATE, BARREL, SIGN)}),
                         sorted(PRIMITIVE_SHAPES))

    def test_a_primitive_the_lint_would_refuse_raises_rather_than_meshes(self):
        """Loud, and it names the lint code that should have caught it - so a
        traceback from here is a *lint* bug, attributably."""
        with self.assertRaises(ValueError) as ctx:
            mesh_primitive("m", {"shape": "box", "size": [1, 0, 1]})
        self.assertIn("invalid_primitive_dimensions", str(ctx.exception))

    def test_every_vertex_is_the_format_the_gl_backend_draws(self):
        from lobster.render import gl_backend
        self.assertEqual(gl_backend.VERTEX_STRIDE, MODEL_VERTEX_STRIDE,
                         "two definitions of one format is two formats")
        mesh = mesh_primitive("model-crate", CRATE)
        self.assertEqual(len(mesh.vertices) % MODEL_VERTEX_STRIDE, 0)
        self.assertEqual(mesh.vertex_count() % 3, 0, "whole triangles")
        for position, normal, tint in vertices_of(mesh):
            self.assertAlmostEqual(sum(c * c for c in normal), 1.0, places=5)
            self.assertTrue(all(0.0 <= c <= 1.0 for c in tint), tint)
            self.assertTrue(all(abs(c) < 10.0 for c in position), position)


# ---------------------------------------------------------------------------
# 2 and 3. Voxel models, and where the origin ends up
# ---------------------------------------------------------------------------

class TestAnchoring(unittest.TestCase):
    """Centred on X and Z, sitting on y = 0 - for rotation, not tidiness."""

    def test_a_box_is_centred_on_xz_and_rests_on_the_ground(self):
        mesh = mesh_primitive("m", {"shape": "box", "size": [2.0, 1.0, 4.0],
                                    "material": 1})
        self.assertEqual(mesh.bounds.minimum, (-1.0, 0.0, -2.0))
        self.assertEqual(mesh.bounds.maximum, (1.0, 1.0, 2.0))

    def test_an_empty_model_anchors_to_a_point_rather_than_dividing_by_zero(self):
        triangles, bounds = anchor([])
        self.assertEqual(triangles, [])
        self.assertEqual(bounds.minimum, (0.0, 0.0, 0.0))

    def test_the_anchor_comes_from_the_mesh_not_the_padded_cube(self):
        """`to_structure` pads a `.vox` up to a legal cube, and the padding is
        empty so it produces no faces. Anchoring on the *grid* instead of the
        mesh would offset every small model by half its padding - silently, and
        only for the small ones."""
        with BuildWorkspace() as ws:
            path = write_vox_file(
                os.path.join(ws.art, "tiny.vox"), (2, 2, 2),
                [(x, y, z, 4) for x in range(2) for y in range(2)
                 for z in range(2)])
            mesh = mesh_voxel_file("model-tiny", path)
        # 2 voxels at 0.25 m is 0.5 m on a side; the padded cube is 8 voxels.
        self.assertEqual(mesh.kind, MODEL_VOXEL)
        self.assertAlmostEqual(mesh.bounds.minimum[0], -0.25, places=6)
        self.assertAlmostEqual(mesh.bounds.maximum[0], 0.25, places=6)
        self.assertAlmostEqual(mesh.bounds.minimum[1], 0.0, places=6)
        self.assertAlmostEqual(mesh.bounds.maximum[1], 0.5, places=6)

    def test_a_voxel_model_is_greedy_meshed_not_face_per_voxel(self):
        """It goes through `StructureMesher`, so a solid cube is six quads and
        not six faces per voxel. If this ever reads 6 * side^2 * 2, something
        has quietly stopped sharing the mesher."""
        with BuildWorkspace() as ws:
            mesh = mesh_voxel_file(
                "model-cube", cube_vox(os.path.join(ws.art, "cube.vox"),
                                       side=8, material=7))
        self.assertEqual(mesh.triangle_count(), 12,
                         "a solid 8-cube is one micro-chunk: six merged quads")


# ---------------------------------------------------------------------------
# 4. The palette, resolved at build time
# ---------------------------------------------------------------------------

class TestPalette(unittest.TestCase):

    def test_a_voxel_model_uses_the_palette_in_its_own_file(self):
        """`write_vox_file` writes a greyscale ramp, so entry i is (i, i, i) and
        palette index m resolves to m - 1. Nothing about that ramp resembles
        `DEFAULT_PALETTE`, which is the point: a model that came out grey-brown
        would be one that ignored the author's colours."""
        with BuildWorkspace() as ws:
            mesh = mesh_voxel_file(
                "model-cube", cube_vox(os.path.join(ws.art, "cube.vox"),
                                       side=8, material=7))
        expected = (6 / 255.0, 6 / 255.0, 6 / 255.0)
        tints = {tint for _p, _n, tint in vertices_of(mesh)}
        self.assertEqual(len(tints), 1)
        for channel, want in zip(tints.pop(), expected):
            self.assertAlmostEqual(channel, want, places=5)
        self.assertNotEqual(
            tuple(round(c * 255) for c in expected),
            tuple(DEFAULT_PALETTE[7]),
            "the fixture cannot tell the two palettes apart")

    def test_a_primitive_falls_back_to_the_engine_palette(self):
        """It has no palette of its own, so a primitive and a structure with the
        same material id look the same."""
        mesh = mesh_primitive("model-crate", CRATE)
        want = tuple(c / 255.0 for c in DEFAULT_PALETTE[6])
        for _position, _normal, tint in vertices_of(mesh):
            for channel, expected in zip(tint, want):
                self.assertAlmostEqual(channel, expected, places=5)

    def test_a_file_with_no_rgba_chunk_falls_back_too(self):
        self.assertEqual(tint_of(6, ()),
                         tuple(c / 255.0 for c in DEFAULT_PALETTE[6]))

    def test_a_tint_is_never_the_baked_lightmap(self):
        """A library model is shared by every cell that places it, so it cannot
        carry one cell's light. Full-brightness tints, always."""
        with BuildWorkspace() as ws:
            mesh = mesh_voxel_file(
                "model-cube", cube_vox(os.path.join(ws.art, "cube.vox"),
                                       side=8, material=200))
        for _position, _normal, tint in vertices_of(mesh):
            for channel in tint:
                self.assertAlmostEqual(channel, 199 / 255.0, places=5)


# ---------------------------------------------------------------------------
# The artifact itself
# ---------------------------------------------------------------------------

def a_library():
    return ModelLibrary(
        models={"model-crate": mesh_primitive("model-crate", CRATE),
                "model-barrel": mesh_primitive("model-barrel", BARREL)},
        provenance={"records": ["model-barrel", "model-crate"],
                    "manifest": "world.manifest.json"})


class TestTheFileFormat(unittest.TestCase):

    def round_trip(self, library, ws):
        path = write_library(library, os.path.join(ws.out, LIBRARY_FILENAME))
        return path, read_library(path)

    def test_what_goes_in_comes_out(self):
        with BuildWorkspace() as ws:
            original = a_library()
            _path, back = self.round_trip(original, ws)
            self.assertEqual(back.model_refs(), original.model_refs())
            for ref in original.model_refs():
                self.assertEqual(back.model(ref).vertices,
                                 original.models[ref].vertices)
                self.assertEqual(back.model(ref).kind,
                                 original.models[ref].kind)
                self.assertEqual(back.model(ref).bounds,
                                 original.models[ref].bounds)
            self.assertEqual(back.provenance["manifest"],
                             "world.manifest.json")

    def test_the_header_is_readable_without_the_payload(self):
        with BuildWorkspace() as ws:
            path, _back = self.round_trip(a_library(), ws)
            header = read_library_header(path)
            self.assertEqual(header["format"], LIBRARY_FORMAT)
            self.assertEqual(len(header["models"]), 2)
            self.assertTrue(all("bounds" in m for m in header["models"]))

    def test_an_empty_library_is_a_legal_file(self):
        with BuildWorkspace() as ws:
            _path, back = self.round_trip(ModelLibrary(), ws)
            self.assertEqual(back.model_refs(), [])
            self.assertEqual(back.nbytes(), 0)

    def test_a_missing_model_names_what_is_there(self):
        with self.assertRaises(ModelLibraryError) as ctx:
            a_library().model("model-nope")
        self.assertIn("model-nope", str(ctx.exception))
        self.assertIn("model-crate", str(ctx.exception))

    # -- the ways a file can be wrong ---------------------------------------
    def corrupt(self, ws, mutate):
        path = write_library(a_library(), os.path.join(ws.out, LIBRARY_FILENAME))
        with open(path, "rb") as f:
            raw = f.read()
        with open(path, "wb") as f:
            f.write(mutate(raw))
        return path

    def test_a_file_that_is_not_a_library_says_so(self):
        with BuildWorkspace() as ws:
            path = self.corrupt(ws, lambda raw: b"NOTALIB!" + raw[8:])
            with self.assertRaises(ModelLibraryError) as ctx:
                read_library(path)
            self.assertIn("bad magic", str(ctx.exception))

    def test_a_truncated_payload_is_a_checksum_failure(self):
        with BuildWorkspace() as ws:
            path = self.corrupt(ws, lambda raw: raw[:-64])
            with self.assertRaises(ModelLibraryError) as ctx:
                read_library(path)
            self.assertIn(os.path.basename(path), str(ctx.exception))

    def test_a_flipped_byte_is_caught(self):
        def flip(raw):
            body = bytearray(raw)
            body[-1] ^= 0xFF
            return bytes(body)

        with BuildWorkspace() as ws:
            path = self.corrupt(ws, flip)
            with self.assertRaises(ModelLibraryError) as ctx:
                read_library(path)
            self.assertIn("checksum", str(ctx.exception))

    def test_a_future_version_is_refused_rather_than_guessed_at(self):
        def bump(raw):
            text = raw.decode("utf-8", "replace")
            return text.replace('"format_version":1',
                                '"format_version":2').encode("utf-8", "replace")

        with BuildWorkspace() as ws:
            path = self.corrupt(ws, bump)
            with self.assertRaises(ModelLibraryError) as ctx:
                read_library_header(path)
            self.assertIn("Rebuild", str(ctx.exception))

    def test_a_declared_vertex_count_that_disagrees_with_the_blob(self):
        """The header and the payload are two statements about one thing, and a
        reader that trusts only the first would hand out half a model.

        33 and not 35: 35 is not whole triangles either, so it would trip the
        *other* guard and this test would pass with this one deleted. It did,
        the first time it was written."""
        def lie(raw):
            return raw.replace(b'"vertex_count":36', b'"vertex_count":33')

        with BuildWorkspace() as ws:
            path = self.corrupt(ws, lie)
            with self.assertRaises(ModelLibraryError) as ctx:
                read_library(path)
            self.assertIn("blob is", str(ctx.exception))
            self.assertIn("model-crate", str(ctx.exception))

    def test_a_vertex_count_that_is_not_whole_triangles(self):
        def lie(raw):
            return raw.replace(b'"vertex_count":36', b'"vertex_count":35')

        with BuildWorkspace() as ws:
            path = self.corrupt(ws, lie)
            with self.assertRaises(ModelLibraryError) as ctx:
                read_library(path)
            self.assertIn("whole triangles", str(ctx.exception))

    def test_the_magic_is_not_the_cell_bundle_s(self):
        from lobster.bundle import MAGIC
        self.assertNotEqual(LIBRARY_MAGIC, MAGIC)
        self.assertEqual(len(LIBRARY_MAGIC), len(MAGIC))


# ---------------------------------------------------------------------------
# 5 and 6. The build, end to end
# ---------------------------------------------------------------------------

def model_ops():
    return demo_world_ops() + [
        C("model-crate", "Model", primitive=CRATE),
        C("model-barrel", "Model", primitive=BARREL),
    ]


def build_with_models(ws, *, cells=None, ops=None, models=None, write=True):
    slab_vox(os.path.join(ws.art, "slab.vox"))
    cube_vox(os.path.join(ws.art, "cube.vox"))
    package = ws.write_package("world.models", ops or model_ops())
    extra = {"models": list(models)} if models else {}
    path = ws.write_manifest(cells or demo_manifest_cells(), [package], **extra)
    return build_from_file(path, out_dir=ws.out, write=write)


class TestTheBuildWritesOne(unittest.TestCase):

    def test_the_library_lands_beside_the_cells(self):
        with BuildWorkspace() as ws:
            report = build_with_models(ws)
            self.assertTrue(report.ok(), report.errors())
            path = os.path.join(ws.out, LIBRARY_FILENAME)
            self.assertIn(path, report.written)
            library = read_library(path)
            self.assertEqual(library.model_refs(),
                             ["model-barrel", "model-crate"])

    def test_a_primitive_only_world_needs_no_art_directory_entry(self):
        """The claim that earned primitives their place: a placeable object
        that ships with nothing but a record."""
        with BuildWorkspace() as ws:
            report = build_with_models(ws)
            manifest = load_manifest(os.path.join(ws.path,
                                                  "world.manifest.json"))
            self.assertEqual(manifest.models, (),
                             "the fixture accidentally declared a models entry")
            self.assertTrue(report.ok(), report.errors())
            self.assertEqual(
                sorted(read_library(os.path.join(ws.out,
                                                 LIBRARY_FILENAME)).model_refs()),
                ["model-barrel", "model-crate"])

    def test_a_voxel_model_resolves_through_the_manifest(self):
        with BuildWorkspace() as ws:
            report = build_with_models(
                ws,
                ops=demo_world_ops() + [C("model-cube", "Model",
                                          asset_ref="model-cube")],
                models=[{"model_ref": "model-cube", "vox": "cube.vox"}])
            self.assertTrue(report.ok(), report.errors())
            library = read_library(os.path.join(ws.out, LIBRARY_FILENAME))
            self.assertEqual(library.model("model-cube").kind, MODEL_VOXEL)
            self.assertEqual(library.model("model-cube").triangle_count(), 12)

    def test_fifty_placements_are_one_mesh(self):
        """ASSET_SCOPE §2's whole argument. If this ever scales with the
        placements, the library has quietly become a per-cell bake."""
        props = [{"prop_id": "crate-%d" % i, "model_ref": "model-crate",
                  "transform": {"position": [4 + i * 0.1, 0, 4]}}
                 for i in range(50)]
        with BuildWorkspace() as ws:
            one = build_with_models(ws)
            bytes_with_none = one.library["bytes"]
        with BuildWorkspace() as ws:
            many = build_with_models(ws, cells=demo_manifest_cells(props=props))
            self.assertTrue(many.ok(), many.errors())
            self.assertEqual(many.library["bytes"], bytes_with_none)
            self.assertEqual(many.library["models"], 2)

    def test_the_report_says_what_each_model_cost(self):
        with BuildWorkspace() as ws:
            report = build_with_models(ws)
            by_model = {m["model_ref"]: m for m in report.library["by_model"]}
            self.assertEqual(by_model["model-crate"]["kind"], MODEL_PRIMITIVE)
            self.assertEqual(by_model["model-crate"]["triangles"], 12)
            self.assertEqual(report.library["triangles"],
                             sum(m["triangles"] for m in by_model.values()))
            self.assertIn("library", report.to_dict())

    def test_it_is_reproducible_byte_for_byte(self):
        """D1: derived, reproducible from content, safe to delete. A library
        that differed between two builds of the same world would make every
        rebuild look like a change."""
        blobs = []
        for _ in range(2):
            with BuildWorkspace() as ws:
                build_with_models(ws)
                with open(os.path.join(ws.out, LIBRARY_FILENAME), "rb") as f:
                    blobs.append(f.read())
        self.assertEqual(blobs[0], blobs[1])

    def test_a_dry_run_writes_no_library(self):
        with BuildWorkspace() as ws:
            report = build_with_models(ws, write=False)
            self.assertTrue(report.ok(), report.errors())
            self.assertTrue(report.library["models"], "it still meshed")
            self.assertEqual(report.written, [])
            self.assertFalse(os.path.isdir(ws.out) and os.listdir(ws.out))


class TestAModelThatWouldNotAppear(unittest.TestCase):
    """The failure this whole step exists to move to build time."""

    def empty_vox(self, ws):
        return write_vox_file(os.path.join(ws.art, "empty.vox"), (4, 4, 4), [])

    def test_a_model_that_meshes_to_nothing_fails_the_build(self):
        with BuildWorkspace() as ws:
            self.empty_vox(ws)
            report = build_with_models(
                ws,
                ops=demo_world_ops() + [C("model-ghost", "Model",
                                          asset_ref="model-ghost")],
                models=[{"model_ref": "model-ghost", "vox": "empty.vox"}])
            self.assertFalse(report.ok())
            codes = [f["code"] for f in report.errors()]
            self.assertEqual(codes, ["model_meshes_to_nothing"])
            self.assertEqual(report.errors()[0]["record_id"], "model-ghost")

    def test_nothing_at_all_is_written_when_a_model_is_broken(self):
        """Not the library, and not the cells either: a world that fails is not
        baked partly. The library is built before any cell for exactly this."""
        with BuildWorkspace() as ws:
            self.empty_vox(ws)
            build_with_models(
                ws,
                ops=demo_world_ops() + [C("model-ghost", "Model",
                                          asset_ref="model-ghost")],
                models=[{"model_ref": "model-ghost", "vox": "empty.vox"}])
            self.assertFalse(os.path.isdir(ws.out) and os.listdir(ws.out),
                             "a cell was baked past a broken model")

    def test_an_unreadable_vox_names_the_record_as_well_as_the_file(self):
        with BuildWorkspace() as ws:
            with open(os.path.join(ws.art, "junk.vox"), "wb") as f:
                f.write(b"not a vox file at all")
            report = build_with_models(
                ws,
                ops=demo_world_ops() + [C("model-junk", "Model",
                                          asset_ref="model-junk")],
                models=[{"model_ref": "model-junk", "vox": "junk.vox"}])
            self.assertFalse(report.ok())
            finding = report.errors()[0]
            self.assertEqual(finding["code"], "model_meshing_failed")
            self.assertEqual(finding["record_id"], "model-junk")
            self.assertIn("junk.vox", finding["detail"])


class TestTheRuntimeNeverWrites(unittest.TestCase):

    def test_the_reader_is_in_lobster_and_the_writer_is_not(self):
        """CONTRACT §5 invariant 1, applied to the new artifact: `lobster/`
        reads bundles, `lobster/build/` makes them."""
        import lobster.model_library as reader
        self.assertFalse(hasattr(reader, "write_library"))
        with open(reader.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('"wb"', source)
        self.assertNotIn('"w"', source)


if __name__ == "__main__":
    unittest.main()
