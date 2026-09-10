"""The differential conformance harness for the accelerator seam (D26, D28).

> **(a) Differential testing, not shared test coverage.** Both paths, same
> inputs, asserted agreement, on generated cases as well as fixtures. "Both pass
> the suite" is not the property - the suite can pass on both while they
> disagree on a case no test covers.

That is D26's first commitment, and this is it. It exists *before* the native
module on purpose: it needs no toolchain, so it cannot become a third
named-but-unwritten thing, and a native implementer gets a target to hit rather
than a description to interpret.

**The seam is two pure kernels.** Everything else stays Python.

| kernel | question |
|---|---|
| `segment_query` | which entities lie within `radius` of this segment |
| `nearest_region` | which bone a ray struck, over an already-filtered capsule list |

**Both sit below the `limb_state` read.** The caller filters severed limbs and
hands over only the capsules that survived, so a native kernel never touches
limb state and the L8 boundary stays in Python permanently.

## What "agree" means

Bit-identical is not achievable and demanding it would be a bug: a native path
that sums in a different order, or in f32, disagrees in the last bits while
being just as correct. So the rules are declared rather than discovered:

* **Membership is exact** - the set of entity ids that came back must match,
  except for candidates sitting within tolerance of the radius, which are
  *admissible either way*. That is the same hazard `_BOUND_EPSILON` exists for:
  a value exactly on a boundary is decided by rounding, and no implementation
  is wrong for landing on either side of it.
* **Distances are compared within `DISTANCE_TOLERANCE_M`.**
* **Order is free where distances are within tolerance of each other**, and
  fixed everywhere else.
* **A region is free where the two nearest bones are within tolerance**, and
  fixed everywhere else.
* **`precise` is exact.** It answers "did a capsule intersect", which is a
  boundary question again - so a case whose best gap is within tolerance of
  zero is skipped as ambiguous rather than compared.

**The authority is the pure-Python path.** Where they differ inside tolerance,
Python's answer is the specified one; outside tolerance the other path is wrong.

## Why this is not vacuous today

With one implementation it compares Python against Python, which would prove
nothing on its own. Three things make it real before any native code exists:

1. **The comparator is mutation-tested.** `tests/test_conformance.py` injects
   dropped candidates, spurious candidates, nudged distances, swapped order and
   flipped flags, and asserts each is caught. A harness that cannot fail is not
   a harness.
2. **Golden vectors are recorded**, so the Python path is pinned against
   accidental change and a native implementer can run the cases without Lobster.
3. **Determinism and order-independence are asserted** on the reference itself:
   same input twice, and shuffled insertion order, must give identical answers.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field as dc_field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from .constants import SPATIAL_GRID_CELL_M
from .geometry import Capsule, Transform, Vec3
from .hittest import nearest_region
from .skeleton import Skeleton, humanoid_region_set
from .spatial import SpatialIndex
from .tiers import ACTIVE, PROJECTILE

#: 0.1 mm. Wide enough for an f32 native path, far below anything a player or a
#: damage number can distinguish. Declared here because D26 requires a stated
#: tolerance rather than one discovered by loosening until the suite passes.
DISTANCE_TOLERANCE_M = 1e-4

#: Which path is right when they differ inside tolerance.
AUTHORITY = "python"

SEGMENT_QUERY = "segment_query"
NEAREST_REGION = "nearest_region"
SEAM_KERNELS = (SEGMENT_QUERY, NEAREST_REGION)


class ConformanceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Cases and results - both plain data, so they serialise
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    """One input to one seam kernel. JSON-serialisable, deliberately."""

    case_id: str
    kernel: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "kernel": self.kernel,
                "payload": self.payload}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Case":
        return cls(case_id=raw["case_id"], kernel=raw["kernel"],
                   payload=raw["payload"])


@dataclass(frozen=True)
class Divergence:
    """One disagreement, named precisely enough to debug from."""

    case_id: str
    kernel: str
    field: str
    reference: Any
    candidate: Any
    detail: str = ""

    def __str__(self) -> str:
        return "{0} [{1}.{2}] reference={3!r} candidate={4!r}{5}".format(
            self.case_id, self.kernel, self.field, self.reference,
            self.candidate, " - " + self.detail if self.detail else "")

    def to_dict(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "kernel": self.kernel,
                "field": self.field, "reference": self.reference,
                "candidate": self.candidate, "detail": self.detail}


# ---------------------------------------------------------------------------
# The reference implementation - the shipping code path, not a copy of it
# ---------------------------------------------------------------------------

def _index_from(entries: Sequence[Sequence[Any]],
                cell_size_m: float) -> SpatialIndex:
    index = SpatialIndex("conformance", cell_size_m=cell_size_m)
    for entity_id, position, tier in entries:
        index.add(entity_id, tuple(position), tier)
    return index


def reference_segment_query(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Calls `SpatialIndex.query_segment` itself. A reference that
    reimplemented the thing it references would drift from it."""
    index = _index_from(payload["entries"], payload["cell_size_m"])
    found = index.query_segment(
        tuple(payload["start"]), tuple(payload["end"]),
        radius=payload["radius"],
        tiers=tuple(payload["tiers"]) if payload.get("tiers") else None)
    return {"hits": [[c.entity_id, c.distance] for c in found]}


def reference_nearest_region(payload: Mapping[str, Any]) -> Dict[str, Any]:
    boxes = [(region, Capsule(tuple(a), tuple(b), r))
             for region, a, b, r in payload["boxes"]]
    region, precise = nearest_region(
        boxes, tuple(payload["origin"]), tuple(payload["direction"]),
        payload["max_distance"])
    return {"region": region, "precise": precise}


REFERENCE: Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]] = {
    SEGMENT_QUERY: reference_segment_query,
    NEAREST_REGION: reference_nearest_region,
}


def run(cases: Iterable[Case],
        impl: Optional[Mapping[str, Callable]] = None) -> List[Dict[str, Any]]:
    """Run every case through an implementation. `None` means the reference."""
    table = REFERENCE if impl is None else impl
    out = []
    for case in cases:
        kernel = table.get(case.kernel)
        if kernel is None:
            raise ConformanceError(
                "{0} does not implement {1!r}; the seam is {2}".format(
                    "the implementation under test", case.kernel,
                    list(SEAM_KERNELS)))
        out.append(kernel(case.payload))
    return out


# ---------------------------------------------------------------------------
# Case generation - adversarial on purpose
# ---------------------------------------------------------------------------

def _crowd(rng: random.Random, count: int, spread: float,
           origin_xz: Tuple[float, float] = (64.0, 64.0)) -> List[List[Any]]:
    return [["e%d" % i,
             [origin_xz[0] + rng.uniform(-spread, spread), 0.0,
              origin_xz[1] + rng.uniform(-spread, spread)],
             ACTIVE if i % 2 else PROJECTILE]
            for i in range(count)]


def generate_cases(seed: int = 20260909, count: int = 80) -> List[Case]:
    """Deterministic cases across the shapes that actually break things.

    The generator is seeded rather than random because a conformance failure
    has to be reproducible by whoever has to fix it - the same argument that
    made the frame budget modelled rather than clocked (D27).
    """
    rng = random.Random(seed)
    cases: List[Case] = []

    # -- segment_query -------------------------------------------------------
    shapes = [
        ("empty", 0, 1.0),            # nothing to find; the walk still runs
        ("lone", 1, 40.0),
        ("sparse", 12, 60.0),
        ("normal", 40, 30.0),
        ("packed", 30, 1.2),          # many bodies inside one bucket
        ("column", 24, 0.0),          # every entity at the same point - all ties
    ]
    for i in range(count // 2):
        label, n, spread = shapes[i % len(shapes)]
        entries = _crowd(rng, n, spread) if spread > 0.0 else [
            ["e%d" % k, [64.0, 0.0, 64.0], ACTIVE if k % 2 else PROJECTILE]
            for k in range(n)]
        # Aim through the crowd most of the time. An early draft fired mostly
        # into empty space - 35 of 40 cases came back with no hits at all,
        # which exercises the grid walk and nothing else. Membership, ordering
        # and distance are the properties most likely to diverge in a native
        # port, so the cases have to actually produce candidates.
        shot = i % 5
        if shot == 4 or not entries:
            start = [-40.0, 1.2, -40.0]      # a deliberate miss, still needed
            end = [-30.0, 1.2, -30.0]
        else:
            through = rng.choice(entries)[1]
            if shot == 0:                    # long shot straight through
                start = [through[0], 1.2, through[2] - 60.0]
                end = [through[0], 1.2, through[2] + 60.0]
            elif shot == 1:                  # short jab at contact range
                start = [through[0] - 1.5, 1.2, through[2] - 1.5]
                end = [through[0] + 1.5, 1.2, through[2] + 1.5]
            elif shot == 2:                  # diagonal across the crowd
                start = [through[0] - 30.0, 1.2, through[2] - 30.0]
                end = [through[0] + 30.0, 1.2, through[2] + 30.0]
            else:                            # grazing pass, near the radius
                start = [through[0] + 2.0, 1.2, through[2] - 20.0]
                end = [through[0] + 2.0, 1.2, through[2] + 20.0]
        tiers = rng.choice([None, [ACTIVE], [ACTIVE, PROJECTILE]])
        cases.append(Case(
            case_id="seg-%03d-%s" % (i, label), kernel=SEGMENT_QUERY,
            payload={"entries": entries, "start": start, "end": end,
                     # >= 1.5 m: entities sit at y=0 and shots at y=1.2, so a
                     # 0.5 m radius can never reach one whatever the XZ layout.
                     "radius": rng.choice([1.5, 2.5, 4.0, 6.0]),
                     "cell_size_m": SPATIAL_GRID_CELL_M, "tiers": tiers}))

    # -- nearest_region ------------------------------------------------------
    for i in range(count - len(cases)):
        stand = (rng.uniform(0.0, 40.0), 0.0, rng.uniform(0.0, 40.0))
        skeleton = Skeleton("subject", humanoid_region_set(),
                            root=Transform(position=stand))
        boxes = skeleton.hitboxes()
        if i % 5 == 0:                       # a severed limb, filtered already
            boxes = [b for b in boxes if b[0] != "left_arm"]
        if i % 7 == 0:                       # nothing left to test at all
            boxes = []
        aim = rng.choice(["head", "torso", "legs", "wide", "behind"])
        if aim == "head":
            target = (stand[0], 1.7, stand[2])
        elif aim == "torso":
            target = (stand[0], 1.1, stand[2])
        elif aim == "legs":
            target = (stand[0], 0.4, stand[2])
        elif aim == "wide":
            target = (stand[0] + 3.0, 1.2, stand[2])
        else:
            target = (stand[0], 1.2, stand[2] - 5.0)
        origin = (stand[0] + rng.uniform(-1.0, 1.0), 1.2, stand[2] - 8.0)
        delta = tuple(target[k] - origin[k] for k in range(3))
        length = math.sqrt(sum(d * d for d in delta)) or 1.0
        # Every fourth case gets a **non-unit** direction. Every generated case
        # used to be normalised, and that blind spot hid a real divergence: the
        # reference judged `precise` over a normalised reach and `region` over a
        # scaled one, so a direction of length 4 measured two different rays
        # (D41). Nothing in the tree passes a non-unit direction, which is
        # exactly why nothing caught it.
        scale_by = rng.choice([1.0, 1.0, 1.0, rng.uniform(0.25, 4.0)])
        cases.append(Case(
            case_id="reg-%03d-%s" % (i, aim), kernel=NEAREST_REGION,
            payload={"boxes": [[r, list(c.a), list(c.b), c.radius]
                               for r, c in boxes],
                     "origin": list(origin),
                     "direction": [d / length * scale_by for d in delta],
                     "max_distance": 40.0}))
    return cases


# ---------------------------------------------------------------------------
# Comparison - where the tolerance rules live
# ---------------------------------------------------------------------------

def _close(a: float, b: float, tol: float = DISTANCE_TOLERANCE_M) -> bool:
    return abs(a - b) <= tol


def _compare_segment_query(case: Case, ref: Mapping[str, Any],
                           cand: Mapping[str, Any]) -> List[Divergence]:
    out: List[Divergence] = []
    radius = case.payload["radius"]
    ref_hits = {eid: d for eid, d in ref["hits"]}
    cand_hits = {eid: d for eid, d in cand["hits"]}

    def boundary(eid: str, hits: Mapping[str, float]) -> bool:
        """A candidate sitting on the radius may fall either way."""
        return eid in hits and _close(hits[eid], radius)

    for eid in sorted(set(ref_hits) - set(cand_hits)):
        if boundary(eid, ref_hits):
            continue
        out.append(Divergence(
            case.case_id, case.kernel, "hits.missing", eid, None,
            "reference found it at %.6f m, radius %.3f m"
            % (ref_hits[eid], radius)))
    for eid in sorted(set(cand_hits) - set(ref_hits)):
        if boundary(eid, cand_hits):
            continue
        out.append(Divergence(
            case.case_id, case.kernel, "hits.spurious", None, eid,
            "not in the reference set"))
    for eid in sorted(set(ref_hits) & set(cand_hits)):
        if not _close(ref_hits[eid], cand_hits[eid]):
            out.append(Divergence(
                case.case_id, case.kernel, "hits.distance",
                ref_hits[eid], cand_hits[eid],
                "%s, tolerance %g m" % (eid, DISTANCE_TOLERANCE_M)))

    # Order is fixed only where the distances are separated by more than the
    # tolerance. Two bodies at the same range may come back in either order.
    ref_order = [eid for eid, _ in ref["hits"]]
    cand_order = [eid for eid, _ in cand["hits"]]
    if set(ref_order) == set(cand_order):
        position = {eid: i for i, eid in enumerate(cand_order)}
        for i in range(len(ref_order) - 1):
            a, b = ref_order[i], ref_order[i + 1]
            if _close(ref_hits[a], ref_hits[b]):
                continue
            if position[a] > position[b]:
                out.append(Divergence(
                    case.case_id, case.kernel, "hits.order", ref_order,
                    cand_order,
                    "%s at %.6f m must precede %s at %.6f m"
                    % (a, ref_hits[a], b, ref_hits[b])))
                break
    return out


def _region_is_ambiguous(payload: Mapping[str, Any]) -> bool:
    """Would two implementations be entitled to disagree here?

    Yes when the two nearest bones are within tolerance of each other, and yes
    when the best gap is within tolerance of zero - a ray grazing a capsule is
    the `_BOUND_EPSILON` problem again, and `precise` flips on it.
    """
    from .geometry import segment_segment_distance
    boxes = [(r, Capsule(tuple(a), tuple(b), rad))
             for r, a, b, rad in payload["boxes"]]
    if len(boxes) < 1:
        return False
    origin = tuple(payload["origin"])
    far = tuple(origin[k] + payload["direction"][k] * payload["max_distance"]
                for k in range(3))
    gaps = sorted(segment_segment_distance(origin, far, c.a, c.b) - c.radius
                  for _, c in boxes)
    if _close(gaps[0], 0.0):
        return True
    return len(gaps) > 1 and _close(gaps[0], gaps[1])


def _compare_nearest_region(case: Case, ref: Mapping[str, Any],
                            cand: Mapping[str, Any]) -> List[Divergence]:
    if _region_is_ambiguous(case.payload):
        return []
    out: List[Divergence] = []
    if ref["region"] != cand["region"]:
        out.append(Divergence(
            case.case_id, case.kernel, "region", ref["region"],
            cand["region"], "unambiguous case - the bones are not equidistant"))
    if bool(ref["precise"]) != bool(cand["precise"]):
        out.append(Divergence(
            case.case_id, case.kernel, "precise", ref["precise"],
            cand["precise"], "a capsule was either intersected or it was not"))
    return out


_COMPARATORS = {SEGMENT_QUERY: _compare_segment_query,
                NEAREST_REGION: _compare_nearest_region}


def compare(cases: Sequence[Case], reference: Sequence[Mapping[str, Any]],
            candidate: Sequence[Mapping[str, Any]]) -> List[Divergence]:
    """Every way the two runs disagree, ignoring the ways they may."""
    if not (len(cases) == len(reference) == len(candidate)):
        raise ConformanceError(
            "case/result count mismatch: {0} cases, {1} reference results, "
            "{2} candidate results".format(
                len(cases), len(reference), len(candidate)))
    out: List[Divergence] = []
    for case, ref, cand in zip(cases, reference, candidate):
        out.extend(_COMPARATORS[case.kernel](case, ref, cand))
    return out


# ---------------------------------------------------------------------------
# Golden vectors
# ---------------------------------------------------------------------------

def build_vectors(seed: int = 20260909, count: int = 80) -> Dict[str, Any]:
    cases = generate_cases(seed, count)
    return {"format": "lobster-conformance/1",
            "seed": seed,
            "authority": AUTHORITY,
            "distance_tolerance_m": DISTANCE_TOLERANCE_M,
            "kernels": list(SEAM_KERNELS),
            "cases": [c.to_dict() for c in cases],
            "expected": run(cases)}


def load_vectors(path: str) -> Tuple[List[Case], List[Dict[str, Any]]]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if raw.get("format") != "lobster-conformance/1":
        raise ConformanceError(
            "{0}: not a conformance vector file (format={1!r})".format(
                path, raw.get("format")))
    return [Case.from_dict(c) for c in raw["cases"]], list(raw["expected"])


def dump_vectors(seed: int = 20260909, count: int = 80) -> str:
    """The vector file as text. **This module never writes it.**

    `test_contract.py::test_no_private_save_format_anywhere` forbids anything in
    `lobster/` from opening a file for writing, and it caught the first draft of
    this function doing exactly that. The invariant is right and the fix is not
    a carve-out: the harness produces the bytes, and whoever wants them on disk
    writes them. Serialisation is data; persistence is somebody else's call (L5).
    """
    return json.dumps(build_vectors(seed, count), indent=1,
                      sort_keys=True) + "\n"
