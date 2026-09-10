"""The differential harness, tested as an instrument (DECISIONS.md D28).

A conformance harness with one implementation compares Python against Python
and passes forever. That is worth nothing on its own, so most of this file is
not "do the two paths agree" - it is **"can this harness tell when they don't"**.

Every rule the comparator declares gets an injected violation and has to catch
it, and every latitude it grants gets an injected difference and has to allow
it. If a native kernel is written later, these are the tests that make its
green run mean something.
"""

from __future__ import annotations

import copy
import struct
import json
import os
import random
import tempfile
import unittest

from lobster.conformance import (AUTHORITY, DISTANCE_TOLERANCE_M,
                                 INSTANCE_FLOATS, NEAREST_REGION, PLACE_BATCH,
                                 SEAM_KERNELS, SEGMENT_QUERY, UNIT_TOLERANCE,
                                 Case, ConformanceError, build_vectors,
                                 _region_is_ambiguous, compare,
                                 dump_vectors, generate_cases, load_vectors,
                                 run)
from lobster.spatial import SpatialIndex
from lobster.tiers import ACTIVE, PROJECTILE

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VECTORS = os.path.join(REPO_ROOT, "conformance", "vectors.json")


class ConformanceFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cases = generate_cases()
        cls.reference = run(cls.cases)

    def mutate(self, predicate, change):
        """Copy the reference results, change one that matches, return both."""
        candidate = copy.deepcopy(self.reference)
        for i, (case, result) in enumerate(zip(self.cases, self.reference)):
            if predicate(case, result):
                change(candidate[i], case)
                return i, candidate
        self.fail("no generated case matched the predicate - the generator no "
                  "longer covers the shape this test is about")


class TestTheHarnessCanFail(ConformanceFixture):
    """Every declared rule, violated on purpose."""

    def test_a_dropped_candidate_is_caught(self):
        def has_hits(case, r):
            return case.kernel == SEGMENT_QUERY and len(r["hits"]) >= 1
        i, candidate = self.mutate(has_hits, lambda r, c: r["hits"].pop(0))
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertEqual(found[0].field, "hits.missing")
        self.assertEqual(found[0].case_id, self.cases[i].case_id)

    def test_a_spurious_candidate_is_caught(self):
        def any_seg(case, r):
            return case.kernel == SEGMENT_QUERY
        _, candidate = self.mutate(
            any_seg, lambda r, c: r["hits"].append(["ghost", 1.0]))
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertEqual(found[0].field, "hits.spurious")
        self.assertEqual(found[0].candidate, "ghost")

    def test_a_distance_outside_tolerance_is_caught(self):
        def has_hits(case, r):
            return case.kernel == SEGMENT_QUERY and len(r["hits"]) >= 1

        def nudge(r, c):
            r["hits"][0][1] += DISTANCE_TOLERANCE_M * 10.0
        _, candidate = self.mutate(has_hits, nudge)
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertEqual(found[0].field, "hits.distance")

    def test_a_reordering_of_separated_candidates_is_caught(self):
        def separated(case, r):
            if case.kernel != SEGMENT_QUERY or len(r["hits"]) < 2:
                return False
            return abs(r["hits"][0][1] - r["hits"][1][1]) > DISTANCE_TOLERANCE_M

        def swap(r, c):
            r["hits"][0], r["hits"][1] = r["hits"][1], r["hits"][0]
        _, candidate = self.mutate(separated, swap)
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertEqual(found[0].field, "hits.order")

    def test_a_changed_region_is_caught(self):
        def resolved(case, r):
            # Must be unambiguous, or the comparator is *right* to allow the
            # change and this test would be asserting the opposite of the rule.
            return (case.kernel == NEAREST_REGION
                    and r["region"] not in (None, "head")
                    and not _region_is_ambiguous(case.payload))

        def rename(r, c):
            r["region"] = "head"
        _, candidate = self.mutate(resolved, rename)
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertEqual(found[0].field, "region")

    def test_a_flipped_precise_flag_is_caught(self):
        def resolved(case, r):
            return (case.kernel == NEAREST_REGION and r["region"] is not None
                    and not _region_is_ambiguous(case.payload))

        def flip(r, c):
            r["precise"] = not r["precise"]
        _, candidate = self.mutate(resolved, flip)
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertIn("precise", [d.field for d in found])

    def test_a_missing_kernel_is_refused_rather_than_skipped(self):
        with self.assertRaises(ConformanceError) as ctx:
            run(self.cases, impl={SEGMENT_QUERY: lambda p: {"hits": []}})
        self.assertIn(NEAREST_REGION, str(ctx.exception))

    def test_mismatched_result_counts_are_refused(self):
        with self.assertRaises(ConformanceError):
            compare(self.cases, self.reference, self.reference[:-1])


class TestTheHarnessAllowsWhatItSaidItWould(ConformanceFixture):
    """The other half. A comparator that catches everything is just as useless
    as one that catches nothing - it would fail every honest native port."""

    def test_a_distance_inside_tolerance_is_allowed(self):
        def has_hits(case, r):
            return case.kernel == SEGMENT_QUERY and len(r["hits"]) >= 1

        def nudge(r, c):
            r["hits"][0][1] += DISTANCE_TOLERANCE_M * 0.1
        _, candidate = self.mutate(has_hits, nudge)
        self.assertEqual(compare(self.cases, self.reference, candidate), [],
                         "a last-bits difference must not fail a port")

    def test_a_candidate_sitting_on_the_radius_may_fall_either_way(self):
        """The `_BOUND_EPSILON` hazard, as a conformance rule.

        An entity at exactly `radius` from the segment is included or excluded
        by float rounding. Neither answer is wrong, and a harness that insisted
        on one would be enforcing an accident.
        """
        radius = 2.0
        case = Case(case_id="boundary", kernel=SEGMENT_QUERY, payload={
            "entries": [["edge", [radius, 0.0, 5.0], ACTIVE]],
            "start": [0.0, 0.0, 0.0], "end": [0.0, 0.0, 10.0],
            "radius": radius, "cell_size_m": 2.5, "tiers": None})
        reference = run([case])
        self.assertEqual(len(reference[0]["hits"]), 1,
                         "fixture must actually sit on the boundary")
        self.assertEqual(compare([case], reference, [{"hits": []}]), [],
                         "excluding a candidate exactly on the radius is a "
                         "legal answer")

    def test_equidistant_bones_may_resolve_either_way(self):
        """Ties in `nearest_region` go to the earlier capsule, which is an
        artefact of iteration order rather than a fact about geometry. A
        native kernel is not wrong for breaking the tie the other way."""
        a = [-1.0, 1.0, 0.0]
        b = [1.0, 1.0, 0.0]
        case = Case(case_id="tie", kernel=NEAREST_REGION, payload={
            "boxes": [["left_arm", a, [a[0], 0.0, 0.0], 0.2],
                      ["right_arm", b, [b[0], 0.0, 0.0], 0.2]],
            "origin": [0.0, 1.0, -5.0], "direction": [0.0, 0.0, 1.0],
            "max_distance": 20.0})
        reference = run([case])
        other = "right_arm" if reference[0]["region"] == "left_arm" else "left_arm"
        candidate = [{"region": other, "precise": reference[0]["precise"]}]
        self.assertEqual(compare([case], reference, candidate), [],
                         "a symmetric tie has no right answer to enforce")


class TestPropertiesOfTheReferenceItself(ConformanceFixture):
    """Things any implementation must satisfy, checked on the one that exists."""

    def test_the_reference_agrees_with_itself(self):
        self.assertEqual(compare(self.cases, self.reference, run(self.cases)),
                         [])

    def test_the_same_input_gives_the_same_answer_twice(self):
        self.assertEqual(run(self.cases), run(self.cases),
                         "a conformance failure has to be reproducible by "
                         "whoever must fix it")

    def test_the_answer_does_not_depend_on_insertion_order(self):
        """Ties break by entity id, so a shuffled index is the same index.

        Without this, a native kernel that filled its buckets in a different
        order would fail conformance for a reason that is not a defect.
        """
        rng = random.Random(4)
        shuffled = []
        for case in self.cases:
            if case.kernel != SEGMENT_QUERY:
                shuffled.append(case)
                continue
            payload = copy.deepcopy(case.payload)
            rng.shuffle(payload["entries"])
            shuffled.append(Case(case.case_id, case.kernel, payload))
        self.assertEqual(compare(self.cases, self.reference, run(shuffled)), [])

    def test_the_generated_cases_actually_exercise_the_rules(self):
        """A generator that produced only empty results would make every test
        above vacuous, which is how this suite would rot without noticing."""
        seg = [r for c, r in zip(self.cases, self.reference)
               if c.kernel == SEGMENT_QUERY]
        reg = [r for c, r in zip(self.cases, self.reference)
               if c.kernel == NEAREST_REGION]
        self.assertGreaterEqual(sum(1 for r in seg if r["hits"]), 10,
                                "membership and distance are barely tested")
        self.assertGreaterEqual(sum(1 for r in seg if len(r["hits"]) >= 2), 5,
                                "ordering is barely tested")
        self.assertGreaterEqual(sum(1 for r in seg if not r["hits"]), 5,
                                "the empty-result path is barely tested")
        self.assertGreaterEqual(len({r["region"] for r in reg}), 4,
                                "region selection is barely tested")
        self.assertTrue(any(r["precise"] for r in reg))
        self.assertTrue(any(not r["precise"] for r in reg))


# ---------------------------------------------------------------------------
# place_batch (D53)
# ---------------------------------------------------------------------------

def _instance_floats(result, slot):
    """The seventeen floats of the `slot`-th visible placement."""
    return list(struct.unpack_from("%df" % INSTANCE_FLOATS,
                                   result["instances"],
                                   slot * INSTANCE_FLOATS * 4))


def _rewrite_instance(result, slot, index, value):
    """Change one float of one instance, leaving the rest byte-identical."""
    floats = _instance_floats(result, slot)
    floats[index] = value
    raw = bytearray(result["instances"])
    struct.pack_into("%df" % INSTANCE_FLOATS, raw,
                     slot * INSTANCE_FLOATS * 4, *floats)
    result["instances"] = bytes(raw)


class PlaceBatchFixture(ConformanceFixture):

    def place_cases(self):
        return [(i, c, r) for i, (c, r)
                in enumerate(zip(self.cases, self.reference))
                if c.kernel == PLACE_BATCH]

    def with_visible(self, minimum=1):
        """A case whose placements are not all on a frustum boundary.

        Boundary placements are admissible either way by declared rule, so a
        mutation applied to one would be *correctly* ignored and the test would
        fail for the wrong reason.
        """
        from lobster.conformance import _place_ambiguous
        for i, case, result in self.place_cases():
            solid = [s for s, index in enumerate(result["visible"])
                     if not _place_ambiguous(case.payload, index)]
            if len(solid) >= minimum:
                return i, case, result, solid
        self.fail("no generated case has {0} unambiguous visible "
                  "placement(s)".format(minimum))

    def caught(self, index, candidate):
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found, "the harness accepted a wrong answer")
        self.assertEqual({d.case_id for d in found},
                         {self.cases[index].case_id})
        return found


class TestThePlaceBatchHarnessCanFail(PlaceBatchFixture):
    """Every declared rule for the third kernel, violated on purpose."""

    def test_a_dropped_placement_is_caught(self):
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        slot = solid[0]
        candidate[i]["visible"].pop(slot)
        candidate[i]["centers"].pop(slot)
        candidate[i]["distances"].pop(slot)
        raw = bytearray(candidate[i]["instances"])
        del raw[slot * INSTANCE_FLOATS * 4:(slot + 1) * INSTANCE_FLOATS * 4]
        candidate[i]["instances"] = bytes(raw)
        found = self.caught(i, candidate)
        self.assertIn("visible.missing", {d.field for d in found})

    def test_a_spurious_placement_is_caught(self):
        i, _case, ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        # an index that is genuinely off-screen in this case
        total = len(self.cases[i].payload["placements"]) // 7
        absent = [x for x in range(total) if x not in ref["visible"]]
        if not absent:
            self.skipTest("this case culls nothing")
        candidate[i]["visible"].append(absent[0])
        candidate[i]["centers"].append([0.0, 0.0, 0.0])
        candidate[i]["distances"].append(1.0)
        candidate[i]["instances"] += struct.pack(
            "%df" % INSTANCE_FLOATS, *([0.0] * INSTANCE_FLOATS))
        found = self.caught(i, candidate)
        self.assertIn("visible.spurious", {d.field for d in found})

    def test_a_reordering_is_caught(self):
        i, _case, _ref, solid = self.with_visible(minimum=2)
        candidate = copy.deepcopy(self.reference)
        a, b = solid[0], solid[1]
        for key in ("visible", "centers", "distances"):
            row = candidate[i][key]
            row[a], row[b] = row[b], row[a]
        found = self.caught(i, candidate)
        self.assertIn("visible.order", {d.field for d in found})

    def test_a_centre_outside_tolerance_is_caught(self):
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        candidate[i]["centers"][solid[0]][1] += 0.05
        self.assertIn("centers",
                      {d.field for d in self.caught(i, candidate)})

    def test_a_distance_outside_tolerance_is_caught(self):
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        candidate[i]["distances"][solid[0]] += 0.05
        self.assertIn("distances",
                      {d.field for d in self.caught(i, candidate)})

    def test_a_translated_instance_is_caught(self):
        """Column-major: 12..14 is the translation, in metres."""
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        was = _instance_floats(candidate[i], solid[0])[12]
        _rewrite_instance(candidate[i], solid[0], 12, was + 0.05)
        self.assertIn("instances[12]",
                      {d.field for d in self.caught(i, candidate)})

    def test_a_rotated_instance_is_caught(self):
        """A direction cosine, compared against `UNIT_TOLERANCE` and not
        against a distance - the reason that constant exists."""
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        was = _instance_floats(candidate[i], solid[0])[0]
        _rewrite_instance(candidate[i], solid[0], 0, was - 0.01)
        self.assertIn("instances[0]",
                      {d.field for d in self.caught(i, candidate)})

    def test_a_changed_light_is_caught(self):
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        was = _instance_floats(candidate[i], solid[0])[16]
        _rewrite_instance(candidate[i], solid[0], 16,
                          0.5 if abs(was - 0.5) > 0.1 else 0.9)
        self.assertIn("instances[16]",
                      {d.field for d in self.caught(i, candidate)})

    def test_a_truncated_instance_block_is_caught(self):
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        candidate[i]["instances"] = candidate[i]["instances"][
            :-INSTANCE_FLOATS * 4]
        self.assertTrue(compare(self.cases, self.reference, candidate),
                        "a short instance block was accepted")


class TestThePlaceBatchHarnessAllowsWhatItSaid(PlaceBatchFixture):

    def test_a_centre_inside_tolerance_is_allowed(self):
        i, _case, _ref, solid = self.with_visible()
        candidate = copy.deepcopy(self.reference)
        candidate[i]["centers"][solid[0]][0] += DISTANCE_TOLERANCE_M * 0.5
        self.assertEqual(compare(self.cases, self.reference, candidate), [])

    def test_a_placement_on_a_frustum_plane_may_fall_either_way(self):
        """The declared rule. Built directly rather than fished out of the
        generated cases, so it tests the rule and not the generator."""
        from lobster.camera import Camera
        settings_far = 500.0
        camera = Camera.looking_at((0.0, 0.0, -10.0), (0.0, 0.0, 1.0),
                                   far=settings_far)
        radius = 2.0
        # sitting exactly on the far plane, where a rounding difference decides
        centre_z = -10.0 + settings_far + radius
        payload = {"camera": {"position": [0.0, 0.0, -10.0],
                              "forward": [0.0, 0.0, 1.0], "up": [0.0, 1.0, 0.0],
                              "fov_y_deg": camera.fov_y_deg,
                              "aspect": camera.aspect, "near": camera.near,
                              "far": settings_far},
                   "cell": {"position": [0.0, 0.0, 0.0],
                            "rotation": [0.0, 0.0, 0.0, 1.0]},
                   "radius": radius,
                   "placements": [0.0, 0.0, centre_z, 0.0, 0.0, 0.0, 1.0],
                   "lightmap": None}
        case = Case(case_id="place-boundary", kernel=PLACE_BATCH,
                    payload=payload)
        ref = run([case])
        either_way = [{"visible": [], "centers": [], "distances": [],
                       "instances": b""}]
        self.assertEqual(compare([case], ref, either_way), [],
                         "a sphere resting on the far plane must be "
                         "admissible either way")


class TestThePlaceBatchCasesExerciseTheRules(PlaceBatchFixture):

    def test_the_generator_produces_both_answers(self):
        rows = self.place_cases()
        drawn = sum(len(r["visible"]) for _i, _c, r in rows)
        total = sum(len(c.payload["placements"]) // 7 for _i, c, _r in rows)
        self.assertGreaterEqual(drawn, 40, "culling is barely tested")
        self.assertGreaterEqual(total - drawn, 40,
                                "the culled path is barely tested")

    def test_rotations_and_lightmaps_are_covered(self):
        rows = self.place_cases()
        self.assertTrue(
            any(c.payload["cell"]["rotation"] != [0.0, 0.0, 0.0, 1.0]
                for _i, c, _r in rows),
            "no case rotates the cell, so composition order is untested")
        lightmaps = [c.payload.get("lightmap") for _i, c, _r in rows]
        self.assertTrue(any(m is None for m in lightmaps))
        self.assertTrue(any(isinstance((m or {}).get("data"), bytes)
                            for m in lightmaps), "the bytes path is untested")
        self.assertTrue(any(isinstance((m or {}).get("data"), list)
                            for m in lightmaps), "the sequence path is untested")

    def test_the_light_sampler_agrees_with_the_cell(self):
        """`sample_lightmap` and `ResidentCell.ambient_at` read the same bake.
        Two readers of one format is exactly how they drift."""
        from lobster.budgets import Budget
        from lobster.cell import ResidentCell
        from lobster.conformance import sample_lightmap
        from tests.fixtures import plain_bundle
        import dataclasses
        import random as _random

        rng = _random.Random(4)
        side = 8
        data = bytes(rng.randrange(256) for _ in range(side * side))
        bundle = dataclasses.replace(plain_bundle("c"), lightmap=data,
                                     lightmap_dims=(side, 1, side),
                                     lightmap_voxel_size=1.0)
        cell = ResidentCell(bundle, Budget.declared("c", {}))
        block = {"data": data, "side": side, "voxel_size": 1.0}
        for _ in range(200):
            point = (rng.uniform(-20, 30), 0.0, rng.uniform(-20, 30))
            self.assertAlmostEqual(sample_lightmap(block, point),
                                   cell.ambient_at(point), places=9,
                                   msg="at {0}".format(point))


class TestGoldenVectors(unittest.TestCase):

    def test_the_committed_vectors_match_the_live_reference(self):
        """Pins the Python path. If a geometry change moves an answer, this
        fails and the vectors are regenerated *deliberately* - which is the
        moment to ask whether the change was intended."""
        cases, expected = load_vectors(VECTORS)
        self.assertEqual(compare(cases, expected, run(cases)), [])

    def test_the_vectors_are_self_describing(self):
        with open(VECTORS, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["format"], "lobster-conformance/1")
        self.assertEqual(raw["authority"], AUTHORITY)
        self.assertEqual(raw["distance_tolerance_m"], DISTANCE_TOLERANCE_M)
        self.assertEqual(sorted(raw["kernels"]), sorted(SEAM_KERNELS),
                         "the vector file must name the seam it covers")

    def test_vectors_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(dump_vectors(count=12))
            cases, expected = load_vectors(path)
            # Against the generator rather than a literal: `count` is shared
            # across the kernels now, so a hard-coded 12 was asserting the
            # split rather than the round trip it is named for.
            self.assertEqual([c.case_id for c in cases],
                             [c.case_id for c in generate_cases(count=12)])
            self.assertEqual(compare(cases, expected, run(cases)), [])

    def test_a_foreign_file_is_refused_by_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"cases": [], "expected": []}, handle)
            with self.assertRaises(ConformanceError):
                load_vectors(path)


class TestTheSeamStaysWhereItWasPut(unittest.TestCase):
    """D26 fixed where the seam goes. This is the guard on that."""

    def test_the_seam_is_three_kernels(self):
        self.assertEqual(sorted(SEAM_KERNELS),
                         ["nearest_region", "place_batch", "segment_query"])

    def test_neither_kernel_reads_limb_state(self):
        """The seam sits *below* the limb-state read on purpose, so a native
        kernel can never touch it and the L8 boundary stays in Python."""
        import inspect
        from lobster import conformance, hittest
        self.assertNotIn("purpose=HIT_TEST_ONLY",
                         inspect.getsource(conformance))
        # The docstring names limb_state to explain why it is absent, so the
        # check is against executable code with the docstring stripped - the
        # same distinction `test_limb_state.py` draws for the L8 guard.
        source = inspect.getsource(hittest.nearest_region)
        body = source.split('"""')[2] if source.count('"""') >= 2 else source
        self.assertNotIn("limb_state", body,
                         "the kernel takes capsules that were already "
                         "filtered; it must not do the filtering")

    def test_the_kernel_is_a_function_not_a_method(self):
        """A method body cannot be swapped out; a module-level function can."""
        import inspect
        from lobster import hittest
        self.assertTrue(inspect.isfunction(hittest.nearest_region))
        self.assertIs(hittest.nearest_region,
                      inspect.getmodule(hittest.nearest_region).nearest_region)


if __name__ == "__main__":
    unittest.main()
