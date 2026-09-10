"""The accelerator seam with something actually behind it (D26, D40).

D26 committed to three things and could only deliver two without a second
implementation. This is the third arriving: **differential testing against a
real alternative**, rather than the reference compared with itself.
"""

from __future__ import annotations

import unittest

from lobster.accel import (KERNEL_PREFERENCE, PREFERENCE, AccelError,
                           available, select)
from lobster.conformance import (AUTHORITY, NEAREST_REGION, SEAM_KERNELS,
                                 SEGMENT_QUERY, compare, generate_cases, run)


def numpy_present() -> bool:
    return _present("numpy")


def native_present() -> bool:
    return _present("native")


def _present(name: str) -> bool:
    return any(i["name"] == name and i["available"] for i in available())


class TestTheRegistry(unittest.TestCase):

    def test_python_is_always_available(self):
        found = {i["name"]: i for i in available()}
        self.assertTrue(found["python"]["available"],
                        "the reference is the floor and cannot be missing")

    def test_every_implementation_says_whether_it_can_run(self):
        for entry in available():
            self.assertTrue(entry["detail"], entry["name"])

    def test_asking_for_something_unavailable_raises(self):
        """The same rule `select_backend` follows: a caller who asked for the
        fast path and silently got the slow one blames the wrong layer."""
        with self.assertRaises(AccelError) as ctx:
            select("cuda")
        self.assertIn("cuda", str(ctx.exception))

    def test_selection_covers_every_seam_kernel(self):
        _name, kernels = select()
        self.assertEqual(sorted(kernels), sorted(SEAM_KERNELS))

    def test_preference_is_declared_per_kernel(self):
        """Not one switch. Measurement put numpy on one side of the seam and
        the reference on the other (D40)."""
        self.assertEqual(sorted(KERNEL_PREFERENCE), sorted(SEAM_KERNELS))
        self.assertNotIn("numpy", KERNEL_PREFERENCE[NEAREST_REGION],
                         "numpy is 2.2x slower than the reference over six "
                         "bones; leaving it in would make the seam a loss")
        self.assertIn("numpy", KERNEL_PREFERENCE[SEGMENT_QUERY])
        for kernel, order in KERNEL_PREFERENCE.items():
            self.assertEqual(order[-1], "python",
                             "{0}: the reference must be the floor".format(kernel))
            self.assertEqual(order[0], "native",
                             "{0}: C wins both, measured".format(kernel))

    def test_the_composed_name_does_not_overclaim(self):
        """Calling the mix "native" would be a lie when half of it is the
        reference, and somebody reading a log would draw the wrong conclusion."""
        name, _kernels = select()
        parts = set(name.split("+"))
        for impl in parts:
            self.assertTrue(_present(impl),
                            "{0} named but not available".format(impl))
        if native_present():
            self.assertEqual(name, "native")
        elif numpy_present():
            self.assertEqual(name, "numpy+python")
        else:
            self.assertEqual(name, "python")


@unittest.skipUnless(native_present(), "the C extension is not built here")
class TestTheNativeKernelAgrees(unittest.TestCase):
    """D26 conditions (a) and (c), with a real native module behind them.

    Built locally, differentially tested locally, and built again from source
    in CI on three platforms - which is two independent executions before a
    player sees it, and the opposite of D22's failure mode.
    """

    def kernels(self):
        return select("native")[1]

    def test_it_agrees_on_the_committed_vectors(self):
        cases = generate_cases()
        self.assertEqual(
            compare(cases, run(cases), run(cases, impl=self.kernels())), [])

    def test_it_agrees_across_many_seeds(self):
        kernels = self.kernels()
        for seed in (1, 7, 20260909, 424242, 99991, 2024):
            cases = generate_cases(seed=seed, count=200)
            divergences = compare(cases, run(cases), run(cases, impl=kernels))
            self.assertEqual(divergences, [],
                             "seed {0}: {1}".format(seed, divergences[:3]))

    def test_it_matches_the_tie_break_by_entity_id(self):
        """Sorting by distance alone passes most cases and reorders ties."""
        from lobster.conformance import REFERENCE
        from lobster.tiers import ACTIVE
        payload = {"entries": [["b", [1.0, 0.0, 0.0], ACTIVE],
                               ["a", [1.0, 0.0, 0.0], ACTIVE]],
                   "start": [0.0, 0.0, 0.0], "end": [0.0, 0.0, 4.0],
                   "radius": 2.0, "cell_size_m": 2.5, "tiers": None}
        self.assertEqual(self.kernels()[SEGMENT_QUERY](payload),
                         REFERENCE[SEGMENT_QUERY](payload))

    def test_it_survives_the_degenerate_inputs(self):
        """Empty crowd, empty rig, zero-length ray, zero-length direction -
        the four shapes most likely to segfault a C kernel."""
        from lobster.conformance import REFERENCE
        payloads = [
            (SEGMENT_QUERY, {"entries": [], "start": [0.0, 0.0, 0.0],
                             "end": [1.0, 0.0, 0.0], "radius": 1.0,
                             "cell_size_m": 2.5, "tiers": None}),
            (SEGMENT_QUERY, {"entries": [["a", [0.0, 0.0, 0.0], "ACTIVE"]],
                             "start": [0.0, 0.0, 0.0], "end": [0.0, 0.0, 0.0],
                             "radius": 1.0, "cell_size_m": 2.5,
                             "tiers": None}),
            (NEAREST_REGION, {"boxes": [], "origin": [0.0, 0.0, 0.0],
                              "direction": [0.0, 0.0, 1.0],
                              "max_distance": 10.0}),
            (NEAREST_REGION, {"boxes": [["head", [0.0, 1.0, 0.0],
                                         [0.0, 1.0, 0.0], 0.1]],
                              "origin": [0.0, 0.0, 0.0],
                              "direction": [0.0, 0.0, 0.0],
                              "max_distance": 10.0}),
        ]
        for kernel, payload in payloads:
            self.assertEqual(self.kernels()[kernel](payload),
                             REFERENCE[kernel](payload), payload)

    def test_a_bad_payload_raises_rather_than_crashing(self):
        for payload in ({"entries": "not a list"},
                        {"boxes": [["head", [0.0], [0.0], 0.1]],
                         "origin": [0.0, 0.0, 0.0],
                         "direction": [0.0, 0.0, 1.0], "max_distance": 1.0}):
            with self.assertRaises(Exception):
                for kernel in self.kernels().values():
                    kernel(payload)


@unittest.skipUnless(numpy_present(), "numpy is not installed here")
class TestNumpyAgreesWithTheReference(unittest.TestCase):
    """D26 condition (a): differential testing, not shared test coverage.

    This is the first time the D28 harness has had two genuinely different
    implementations to compare. Everything it asserts about itself - the
    mutation tests, the tolerance rules, the tie latitudes - was written for
    this moment.
    """

    def kernels(self):
        return select("numpy")[1]

    def test_it_agrees_on_the_committed_vectors(self):
        cases = generate_cases()
        self.assertEqual(
            compare(cases, run(cases), run(cases, impl=self.kernels())), [])

    def test_it_agrees_across_many_seeds(self):
        """80 cases is a sample, not a proof. A divergence that only shows up
        on one crowd layout is exactly what this harness exists to catch."""
        kernels = self.kernels()
        for seed in (1, 7, 20260909, 424242, 99991):
            cases = generate_cases(seed=seed, count=200)
            divergences = compare(cases, run(cases), run(cases, impl=kernels))
            self.assertEqual(divergences, [],
                             "seed {0}: {1}".format(seed, divergences[:3]))

    def test_the_reference_stays_authoritative(self):
        """Agreement does not promote the fast path. Where they differ inside
        tolerance, Python's answer is the specified one (D28)."""
        self.assertEqual(AUTHORITY, "python")

    def test_it_matches_the_reference_ordering_exactly(self):
        """Candidates sort by (distance, entity_id). Sorting by distance alone
        passes most cases and reorders ties - the kind of near-miss the
        comparator's order rule was written for."""
        from lobster.tiers import ACTIVE
        payload = {"entries": [["b", [1.0, 0.0, 0.0], ACTIVE],
                               ["a", [1.0, 0.0, 0.0], ACTIVE]],
                   "start": [0.0, 0.0, 0.0], "end": [0.0, 0.0, 4.0],
                   "radius": 2.0, "cell_size_m": 2.5, "tiers": None}
        from lobster.conformance import REFERENCE
        self.assertEqual(self.kernels()[SEGMENT_QUERY](payload),
                         REFERENCE[SEGMENT_QUERY](payload))

    def test_it_handles_an_empty_crowd_and_an_empty_rig(self):
        from lobster.conformance import REFERENCE
        empty_seg = {"entries": [], "start": [0.0, 0.0, 0.0],
                     "end": [1.0, 0.0, 1.0], "radius": 2.0,
                     "cell_size_m": 2.5, "tiers": None}
        empty_rig = {"boxes": [], "origin": [0.0, 0.0, 0.0],
                     "direction": [0.0, 0.0, 1.0], "max_distance": 10.0}
        for kernel, payload in ((SEGMENT_QUERY, empty_seg),
                                (NEAREST_REGION, empty_rig)):
            self.assertEqual(self.kernels()[kernel](payload),
                             REFERENCE[kernel](payload), kernel)


class TestTheSeamKeepsItsPromises(unittest.TestCase):
    """D26's conditions, checked rather than described."""

    def test_core_lobster_imports_no_accelerator(self):
        """L7: the shell drags in no dependency. `numpy` may be installed here
        and must still not be reachable from the runtime path."""
        import os
        import re
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "lobster")
        offenders = []
        for base, _dirs, files in os.walk(root):
            if os.path.basename(base) == "accel":
                continue
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as handle:
                    body = handle.read()
                code = re.sub(r'""".*?"""', "", body, flags=re.S)
                if re.search(r"^\s*import numpy|^\s*from numpy", code,
                             re.M):
                    offenders.append(name)
        self.assertEqual(offenders, [],
                         "these import numpy outside the accel package: "
                         "{0}".format(offenders))

    def test_the_pure_python_path_still_answers_everything(self):
        _name, kernels = select("python")
        cases = generate_cases(count=40)
        self.assertEqual(len(run(cases, impl=kernels)), len(cases))


if __name__ == "__main__":
    unittest.main()
