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
import json
import os
import random
import tempfile
import unittest

from lobster.conformance import (AUTHORITY, DISTANCE_TOLERANCE_M,
                                 NEAREST_REGION, SEAM_KERNELS, SEGMENT_QUERY,
                                 Case, ConformanceError, build_vectors,
                                 compare, dump_vectors, generate_cases,
                                 load_vectors, run)
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
            return case.kernel == NEAREST_REGION and r["region"] not in (
                None, "head")

        def rename(r, c):
            r["region"] = "head"
        _, candidate = self.mutate(resolved, rename)
        found = compare(self.cases, self.reference, candidate)
        self.assertTrue(found)
        self.assertEqual(found[0].field, "region")

    def test_a_flipped_precise_flag_is_caught(self):
        def resolved(case, r):
            return case.kernel == NEAREST_REGION and r["region"] is not None

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
            self.assertEqual(len(cases), 12)
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

    def test_the_seam_is_two_kernels(self):
        self.assertEqual(sorted(SEAM_KERNELS),
                         ["nearest_region", "segment_query"])

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
