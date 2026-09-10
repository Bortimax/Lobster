"""The differential conformance harness for the accelerator seam (D26, D28).

> **(a) Differential testing, not shared test coverage.** Both paths, same
> inputs, asserted agreement, on generated cases as well as fixtures. "Both pass
> the suite" is not the property - the suite can pass on both while they
> disagree on a case no test covers.

That is D26's first commitment, and this is it. It exists *before* the native
module on purpose: it needs no toolchain, so it cannot become a third
named-but-unwritten thing, and a native implementer gets a target to hit rather
than a description to interpret.

**The seam is four pure kernels.** Everything else stays Python.

| kernel | question |
|---|---|
| `segment_query` | which entities lie within `radius` of this segment |
| `pose_capsules` | where this rig's bones are, in world space |
| `nearest_region` | which bone a ray struck, over an already-filtered capsule list |
| `place_batch` | which of these placements are on screen, and their instance data |

**Every one of them sits below the `limb_state` read**, and `pose_capsules` is
the one that had to be designed for it. Deciding *which* bones offer a hitbox is
a read of limb state and therefore policy; placing the bones that survived is
arithmetic. So the caller filters and passes `bones` - the indices that
survived - and the kernel transforms exactly those. A native kernel never sees a
limb id, never sees a state string, and the L8 boundary stays in Python
permanently (D54).

`place_batch` is the third (D53), and its granularity is D26's argument applied
again: one call per resident cell per model per frame. The intermediate between
culling and packing - which placements survived and where they ended up - never
crosses the boundary, because crossing it per placement is the cost. The input
buffer is built **once per residency**, not per frame, for the same reason
`upload_cell` exists: a prop does not move.

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
import struct
from dataclasses import dataclass, field as dc_field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from .camera import Camera
from .constants import SPATIAL_GRID_CELL_M
from .geometry import Capsule, Transform, Vec3, matrix4, pack_matrix4
from .hittest import nearest_region
from .skeleton import Skeleton, humanoid_region_set
from .spatial import SpatialIndex
from .tiers import ACTIVE, PROJECTILE

#: 0.1 mm. Wide enough for an f32 native path, far below anything a player or a
#: damage number can distinguish. Declared here because D26 requires a stated
#: tolerance rather than one discovered by loosening until the suite passes.
DISTANCE_TOLERANCE_M = 1e-4

#: Tolerance for the dimensionless numbers `place_batch` returns - rotation
#: cosines and a 0..1 light level. Declared separately from the metre tolerance
#: because comparing a direction cosine against a distance tolerance is a
#: category error that happens to work while the numbers are near 1.
UNIT_TOLERANCE = 1e-4

#: Which path is right when they differ inside tolerance.
AUTHORITY = "python"

SEGMENT_QUERY = "segment_query"
NEAREST_REGION = "nearest_region"
PLACE_BATCH = "place_batch"
POSE_CAPSULES = "pose_capsules"
SEAM_KERNELS = (SEGMENT_QUERY, NEAREST_REGION, PLACE_BATCH, POSE_CAPSULES)

#: floats per placement in a `place_batch` payload: position, then rotation.
PLACEMENT_FLOATS = 7

#: floats per instance in its result: a column-major 4x4, then the baked light
#: where that instance stands. The same seventeen `gl_backend` uploads.
INSTANCE_FLOATS = 17

#: floats per capsule, in and out of `pose_capsules`: `a`, `b`, radius. The
#: rest pose goes in in this shape and the world-space capsule comes out in it.
CAPSULE_FLOATS = 7

#: `pose_capsules` answers in **float64**, unlike `place_batch`. Its consumer is
#: `nearest_region`, which does its arithmetic in doubles; narrowing to f32 on
#: the way between two CPU kernels would throw away precision to save nothing.
#: `place_batch` narrows because its consumer is a GPU vertex buffer.
CAPSULE_PACK = "d"


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


def camera_from(payload: Mapping[str, Any]) -> Camera:
    """A `Camera` from a `place_batch` payload's camera block.

    The payload carries a camera's *fields*, not its derived basis, so the
    reference builds a real `Camera` and the native path has to reproduce the
    same orthonormalisation. That is deliberate: a payload carrying the basis
    would hide the one derivation most likely to differ.
    """
    cam = payload["camera"]
    return Camera(position=tuple(cam["position"]),
                  forward=tuple(cam["forward"]), up=tuple(cam["up"]),
                  fov_y_deg=float(cam["fov_y_deg"]),
                  aspect=float(cam["aspect"]),
                  near=float(cam["near"]), far=float(cam["far"]))


def sample_lightmap(lightmap: Optional[Mapping[str, Any]],
                    point: Vec3) -> float:
    """`ResidentCell.ambient_at`, over the bytes rather than over a cell.

    Split out so the kernel can be handed a lightmap without being handed a
    `ResidentCell`, and so this file and `cell.py` cannot drift: a test asserts
    they agree on the same bake.
    """
    if not lightmap:
        return 1.0
    data = lightmap.get("data") or b""
    side = int(lightmap.get("side") or 0)
    if not data or side <= 0:
        return 1.0
    voxel = float(lightmap.get("voxel_size") or 1.0) or 1.0
    ix = int(point[0] // voxel)
    iz = int(point[2] // voxel)
    ix = 0 if ix < 0 else (side - 1 if ix >= side else ix)
    iz = 0 if iz < 0 else (side - 1 if iz >= side else iz)
    index = iz * side + ix
    if index >= len(data):
        return 1.0
    return data[index] / 255.0


def reference_place_batch(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Cull a cell's placements and pack what survived.

    Calls the real `Camera.sees_sphere`, the real `Transform.compose` and the
    real `geometry.matrix4`/`pack_matrix4` - a reference that reimplemented any
    of them would drift from the thing it is the reference for.
    """
    camera = camera_from(payload)
    cell = payload["cell"]
    cell_placement = Transform(position=tuple(cell["position"]),
                               rotation=tuple(cell["rotation"]))
    radius = float(payload["radius"])
    flat = payload["placements"]
    lightmap = payload.get("lightmap")

    visible: List[int] = []
    centers: List[List[float]] = []
    distances: List[float] = []
    packed = bytearray()

    for index in range(len(flat) // PLACEMENT_FLOATS):
        base = index * PLACEMENT_FLOATS
        local = Transform(
            position=(float(flat[base]), float(flat[base + 1]),
                      float(flat[base + 2])),
            rotation=(float(flat[base + 3]), float(flat[base + 4]),
                      float(flat[base + 5]), float(flat[base + 6])))
        world = cell_placement.compose(local)
        centre = world.position
        if not camera.sees_sphere(centre, radius):
            continue
        visible.append(index)
        centers.append([centre[0], centre[1], centre[2]])
        distances.append(camera.distance_to(centre))
        packed.extend(pack_matrix4(matrix4(world)))
        packed.extend(struct.pack("f", sample_lightmap(lightmap, centre)))

    return {"visible": visible, "centers": centers, "distances": distances,
            "instances": bytes(packed)}


def reference_pose_capsules(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Where a posed rig's bones are, in world space.

    Composes root and bone transform **once** and applies the result to both
    endpoints, rather than applying two transforms to each endpoint in turn -
    three rotations instead of four, for an answer that is the same to within
    far less than the declared tolerance. `Transform.compose` is asserted
    against applying both in order over random pairs.

    `bones` is the caller's filter and this kernel never questions it. Which
    bones offer a hitbox is a read of `limb_state`, which is policy and stays
    in Python (L8); where the surviving bones *are* is arithmetic.
    """
    root_block = payload["root"]
    root = Transform(position=tuple(root_block["position"]),
                     rotation=tuple(root_block["rotation"]))
    pose = payload["pose"]
    rest = payload["rest"]
    total = len(rest) // CAPSULE_FLOATS
    selected = payload.get("bones")
    if selected is None:
        indices: Sequence[int] = range(total)
    else:
        indices = [int(i) for i in selected]

    out = bytearray()
    for index in indices:
        # Checked rather than trusted, and **negative indices are refused**
        # rather than wrapped. Python indexing would have quietly served the
        # last bone for -1, which is a hitbox belonging to a different limb
        # than the caller asked for - the silent kind of wrong. The native
        # kernel raises here, so the reference has to as well or they disagree
        # on a case no generated payload happens to contain.
        if not 0 <= index < total:
            raise IndexError(
                "bone index {0} is outside this rig's {1} bones".format(
                    index, total))
        p = index * PLACEMENT_FLOATS
        local = Transform(
            position=(float(pose[p]), float(pose[p + 1]), float(pose[p + 2])),
            rotation=(float(pose[p + 3]), float(pose[p + 4]),
                      float(pose[p + 5]), float(pose[p + 6])))
        r = index * CAPSULE_FLOATS
        placed = root.compose(local)
        a = placed.apply((float(rest[r]), float(rest[r + 1]),
                          float(rest[r + 2])))
        b = placed.apply((float(rest[r + 3]), float(rest[r + 4]),
                          float(rest[r + 5])))
        out.extend(struct.pack("7" + CAPSULE_PACK, a[0], a[1], a[2],
                               b[0], b[1], b[2], float(rest[r + 6])))
    return {"capsules": bytes(out)}


REFERENCE: Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]] = {
    SEGMENT_QUERY: reference_segment_query,
    NEAREST_REGION: reference_nearest_region,
    PLACE_BATCH: reference_place_batch,
    POSE_CAPSULES: reference_pose_capsules,
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
    cases.extend(generate_place_batch_cases(seed + 1,
                                            count=max(8, count // 2)))
    cases.extend(generate_pose_capsules_cases(seed + 2,
                                              count=max(8, count // 2)))
    return cases


def _spin(rng: random.Random) -> List[float]:
    axis = [rng.uniform(-1.0, 1.0) for _ in range(3)]
    norm = math.sqrt(sum(c * c for c in axis)) or 1.0
    angle = rng.uniform(-math.pi, math.pi)
    scale = math.sin(angle * 0.5) / norm
    return [axis[0] * scale, axis[1] * scale, axis[2] * scale,
            math.cos(angle * 0.5)]


def _camera_block(position, forward, up=(0.0, 1.0, 0.0), fov=60.0,
                  aspect=16.0 / 9.0, near=0.1, far=500.0):
    return {"position": list(position), "forward": list(forward),
            "up": list(up), "fov_y_deg": fov, "aspect": aspect,
            "near": near, "far": far}


def _lightmap(rng: random.Random, side: int = 8, as_bytes: bool = True):
    data = bytes(rng.randrange(256) for _ in range(side * side))
    return {"data": data if as_bytes else list(data), "side": side,
            "voxel_size": 1.0}


def generate_place_batch_cases(seed: int = 20260910,
                               count: int = 34) -> List[Case]:
    """Cases across the shapes that actually break a cull-and-pack.

    The awkward ones on purpose: spheres straddling each frustum plane, a
    rotated *cell* placement (so composition order matters), rotations on the
    placements themselves (so a dropped quaternion shows), points outside the
    lightmap (so the clamp shows), and a lightmap given as bytes *and* as a
    list, because the native path has a fast route for the first and would
    otherwise never see the second.
    """
    rng = random.Random(seed)
    cases: List[Case] = []

    def add(name, camera, cell, radius, placements, lightmap=None):
        cases.append(Case(
            case_id="place-%s" % name, kernel=PLACE_BATCH,
            payload={"camera": camera, "cell": cell, "radius": radius,
                     "placements": placements, "lightmap": lightmap}))

    here = {"position": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0]}
    ahead = _camera_block((0.0, 2.0, -10.0), (0.0, 0.0, 1.0))
    identity = [0.0, 0.0, 0.0, 1.0]

    add("empty", ahead, here, 1.0, [])
    add("one-visible", ahead, here, 1.0, [0.0, 0.0, 5.0] + identity)
    add("one-behind", ahead, here, 1.0, [0.0, 0.0, -50.0] + identity)
    add("one-beyond-far", ahead, here, 1.0, [0.0, 0.0, 900.0] + identity)
    add("one-far-left", ahead, here, 1.0, [-500.0, 0.0, 5.0] + identity)
    add("zero-radius", ahead, here, 0.0, [0.0, 0.0, 5.0] + identity)
    add("huge-radius", ahead, here, 400.0, [0.0, 0.0, 900.0] + identity)

    # straddling each plane: the sphere is outside but its radius reaches in
    add("straddle-near", ahead, here, 4.0, [0.0, 2.0, -12.0] + identity)
    add("straddle-far", ahead, here, 6.0, [0.0, 2.0, 493.0] + identity)
    add("straddle-side", ahead, here, 6.0, [30.0, 2.0, 20.0] + identity)
    add("straddle-top", ahead, here, 6.0, [0.0, 40.0, 20.0] + identity)

    # a rotated cell, so composing in the wrong order is visible
    for i in range(3):
        cell = {"position": [rng.uniform(-40, 40), rng.uniform(-3, 3),
                             rng.uniform(-40, 40)],
                "rotation": _spin(rng)}
        flat: List[float] = []
        for _ in range(12):
            flat.extend([rng.uniform(-20, 20), rng.uniform(-2, 4),
                         rng.uniform(-20, 20)])
            flat.extend(_spin(rng))
        add("rotated-cell-%d" % i, ahead, cell, 1.0, flat)

    # cameras pointing in awkward directions
    for i, forward in enumerate(((0.0, 0.0, -1.0), (1.0, 0.0, 0.0),
                                 (0.0, 0.999, 0.05), (-0.4, -0.6, 0.7))):
        flat = []
        for _ in range(16):
            flat.extend([rng.uniform(-30, 30), rng.uniform(-5, 5),
                         rng.uniform(-30, 30)])
            flat.extend(identity)
        add("aim-%d" % i, _camera_block((0.0, 2.0, 0.0), forward), here,
            1.2, flat)

    # lightmaps: bytes, list, absent, and points that fall outside it
    crowd: List[float] = []
    for _ in range(20):
        crowd.extend([rng.uniform(-2, 10), 0.0, rng.uniform(-2, 10)])
        crowd.extend(identity)
    add("light-bytes", ahead, here, 1.5, crowd, _lightmap(rng))
    add("light-list", ahead, here, 1.5, crowd, _lightmap(rng, as_bytes=False))
    add("light-none", ahead, here, 1.5, crowd, None)
    add("light-outside", ahead, here, 40.0,
        [-90.0, 0.0, -90.0] + identity + [900.0, 0.0, 900.0] + identity,
        _lightmap(rng, side=4))
    add("light-empty", ahead, here, 1.5, crowd,
        {"data": b"", "side": 0, "voxel_size": 1.0})

    # crowds, which is the case the kernel exists for
    for i, n in enumerate((1, 2, 50, 200)):
        flat = []
        for _ in range(n):
            flat.extend([rng.uniform(-40, 40), rng.uniform(-4, 6),
                         rng.uniform(-10, 60)])
            flat.extend(_spin(rng))
        add("crowd-%d" % i, ahead, here, 1.0, flat, _lightmap(rng, side=16))

    while len(cases) < count:
        flat = []
        for _ in range(rng.randrange(1, 24)):
            flat.extend([rng.uniform(-60, 60), rng.uniform(-8, 8),
                         rng.uniform(-60, 60)])
            flat.extend(_spin(rng))
        add("fuzz-%d" % len(cases),
            _camera_block((rng.uniform(-5, 5), rng.uniform(0, 6),
                           rng.uniform(-20, 0)),
                          (rng.uniform(-1, 1), rng.uniform(-0.4, 0.4), 1.0),
                          fov=rng.uniform(35.0, 100.0),
                          aspect=rng.uniform(1.0, 2.4)),
            {"position": [rng.uniform(-20, 20), 0.0, rng.uniform(-20, 20)],
             "rotation": _spin(rng)},
            rng.uniform(0.2, 4.0), flat,
            _lightmap(rng, side=8) if len(cases) % 2 else None)
    return cases[:count]


def generate_pose_capsules_cases(seed: int = 20260911,
                                 count: int = 30) -> List[Case]:
    """Cases across the shapes that actually break a pose transform.

    The rig this ships with has six bones and no rotation on any of them, which
    would make a dropped bone rotation invisible - so every generated case
    turns something. Severed limbs are a *subset* of the bone list rather than
    a flag, because that is how the caller expresses them and a kernel that
    quietly transformed all of them would still pass a length check on the
    common case where nothing is severed.
    """
    rng = random.Random(seed)
    cases: List[Case] = []

    def add(name, root, pose, rest, bones):
        cases.append(Case(
            case_id="pose-%s" % name, kernel=POSE_CAPSULES,
            payload={"root": root, "pose": pose, "rest": rest,
                     "bones": bones}))

    def rig(n, *, spin=True, span=0.4):
        pose: List[float] = []
        rest: List[float] = []
        for i in range(n):
            pose.extend([rng.uniform(-0.5, 0.5), rng.uniform(0.0, 1.8),
                         rng.uniform(-0.5, 0.5)])
            pose.extend(_spin(rng) if spin else [0.0, 0.0, 0.0, 1.0])
            ax, ay, az = (rng.uniform(-span, span) for _ in range(3))
            rest.extend([ax, ay, az,
                         ax + rng.uniform(-span, span),
                         ay + rng.uniform(0.0, span),
                         az + rng.uniform(-span, span),
                         rng.uniform(0.02, 0.25)])
        return pose, rest

    def at(position=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0, 1.0)):
        return {"position": list(position), "rotation": list(rotation)}

    add("empty", at(), [], [], None)
    pose, rest = rig(1)
    add("one-bone", at(), pose, rest, None)
    add("one-bone-selected", at(), pose, rest, [0])
    add("one-bone-severed", at(), pose, rest, [])

    # the shipped humanoid, at rest and posed
    humanoid = humanoid_region_set()
    flat_rest: List[float] = []
    for bone in humanoid.bones:
        flat_rest.extend(list(bone.a) + list(bone.b) + [bone.radius])
    rest_pose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0] * len(humanoid.bones)
    add("humanoid-rest", at((3.0, 0.0, 4.0)), rest_pose, flat_rest, None)
    spun_pose: List[float] = []
    for _ in humanoid.bones:
        spun_pose.extend([rng.uniform(-0.2, 0.2)] * 3)
        spun_pose.extend(_spin(rng))
    add("humanoid-posed", at((3.0, 0.0, 4.0), _spin(rng)), spun_pose,
        flat_rest, None)
    # one arm off, which is the case the `bones` filter exists for
    add("humanoid-one-limb-gone", at((3.0, 0.0, 4.0)), spun_pose, flat_rest,
        [0, 1, 3, 4, 5])
    add("humanoid-all-gone", at((3.0, 0.0, 4.0)), spun_pose, flat_rest, [])
    add("humanoid-one-left", at((3.0, 0.0, 4.0)), spun_pose, flat_rest, [2])

    # a root that turns, which is where composing in the wrong order shows
    for i in range(4):
        pose, rest = rig(6)
        add("turned-root-%d" % i,
            at((rng.uniform(-60, 60), rng.uniform(-3, 3),
                rng.uniform(-60, 60)), _spin(rng)), pose, rest, None)

    # degenerate and awkward rigs
    add("zero-length-bone", at((1.0, 2.0, 3.0), _spin(rng)),
        [0.0, 0.0, 0.0] + _spin(rng),
        [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.1], None)
    add("zero-radius", at(), [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0], None)
    add("far-from-the-origin", at((6000.0, -400.0, 9000.0), _spin(rng)),
        *rig(4), None)

    # big rigs, and subsets of them
    for i, n in enumerate((12, 40)):
        pose, rest = rig(n)
        add("big-%d" % i, at((rng.uniform(-20, 20), 0.0, rng.uniform(-20, 20)),
                             _spin(rng)), pose, rest, None)
        add("big-subset-%d" % i, at((1.0, 0.0, 2.0), _spin(rng)), pose, rest,
            sorted(rng.sample(range(n), n // 3)))

    while len(cases) < count:
        n = rng.randrange(1, 9)
        pose, rest = rig(n)
        keep = None if rng.random() < 0.5 else sorted(
            rng.sample(range(n), rng.randrange(0, n + 1)))
        add("fuzz-%d" % len(cases),
            at((rng.uniform(-40, 40), rng.uniform(-4, 4),
                rng.uniform(-40, 40)), _spin(rng)), pose, rest, keep)
    return cases[:count]


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


def _place_ambiguous(payload: Mapping[str, Any], index: int) -> bool:
    """Is this placement sitting on a frustum plane?

    `sees_sphere` is four comparisons, and a sphere within tolerance of
    equality in any of them may be decided either way by rounding. The same
    hazard `_BOUND_EPSILON` exists for, and the same rule `segment_query` uses
    for a candidate sitting on the radius.
    """
    camera = camera_from(payload)
    radius = float(payload["radius"])
    flat = payload["placements"]
    cell = payload["cell"]
    base = index * PLACEMENT_FLOATS
    world = Transform(position=tuple(cell["position"]),
                      rotation=tuple(cell["rotation"])).compose(
        Transform(position=(float(flat[base]), float(flat[base + 1]),
                            float(flat[base + 2])),
                  rotation=(float(flat[base + 3]), float(flat[base + 4]),
                            float(flat[base + 5]), float(flat[base + 6]))))
    view = camera.to_view(world.position)
    tan_x, tan_y = camera.tan_half_fov()
    margins = (
        (view[2] + radius) - camera.near,
        camera.far - (view[2] - radius),
        view[2] * tan_x + radius * math.sqrt(1.0 + tan_x * tan_x)
        - abs(view[0]),
        view[2] * tan_y + radius * math.sqrt(1.0 + tan_y * tan_y)
        - abs(view[1]),
    )
    return any(abs(m) <= DISTANCE_TOLERANCE_M for m in margins)


def _instances_by_index(result: Mapping[str, Any]) -> Dict[int, Tuple[float, ...]]:
    """Instance floats keyed by the placement they belong to.

    Keyed rather than positional because the two sides may legitimately
    disagree about one boundary placement, and a positional comparison would
    then report every instance after it as wrong.
    """
    raw = result.get("instances") or b""
    if isinstance(raw, str):                     # a vector file that was not decoded
        raw = bytes.fromhex(raw)
    out: Dict[int, Tuple[float, ...]] = {}
    stride = INSTANCE_FLOATS * 4
    for slot, index in enumerate(result.get("visible") or ()):
        offset = slot * stride
        if offset + stride > len(raw):
            # Short block: the answer is wrong, and the *comparator* must say
            # so rather than raise. `struct.unpack_from` threw here on the
            # first truncation test, which would have turned a caught mutant
            # into an error in the harness.
            continue
        out[int(index)] = struct.unpack_from("%df" % INSTANCE_FLOATS, raw,
                                             offset)
    return out


def _compare_place_batch(case: Case, ref: Mapping[str, Any],
                         cand: Mapping[str, Any]) -> List[Divergence]:
    out: List[Divergence] = []
    ref_visible = [int(i) for i in ref["visible"]]
    cand_visible = [int(i) for i in cand["visible"]]

    for index in sorted(set(ref_visible) - set(cand_visible)):
        if _place_ambiguous(case.payload, index):
            continue
        out.append(Divergence(case.case_id, case.kernel, "visible.missing",
                              index, None, "the reference drew it"))
    for index in sorted(set(cand_visible) - set(ref_visible)):
        if _place_ambiguous(case.payload, index):
            continue
        out.append(Divergence(case.case_id, case.kernel, "visible.spurious",
                              None, index, "the reference culled it"))

    shared = set(ref_visible) & set(cand_visible)
    if ([i for i in ref_visible if i in shared]
            != [i for i in cand_visible if i in shared]):
        out.append(Divergence(case.case_id, case.kernel, "visible.order",
                              ref_visible, cand_visible,
                              "placements come back in input order"))

    ref_centre = {i: ref["centers"][s] for s, i in enumerate(ref_visible)}
    cand_centre = {i: cand["centers"][s] for s, i in enumerate(cand_visible)}
    ref_dist = {i: ref["distances"][s] for s, i in enumerate(ref_visible)}
    cand_dist = {i: cand["distances"][s] for s, i in enumerate(cand_visible)}
    for index in sorted(shared):
        for axis in range(3):
            if not _close(ref_centre[index][axis], cand_centre[index][axis]):
                out.append(Divergence(
                    case.case_id, case.kernel, "centers", ref_centre[index],
                    cand_centre[index], "placement %d" % index))
                break
        if not _close(ref_dist[index], cand_dist[index]):
            out.append(Divergence(case.case_id, case.kernel, "distances",
                                  ref_dist[index], cand_dist[index],
                                  "placement %d" % index))

    ref_inst = _instances_by_index(ref)
    cand_inst = _instances_by_index(cand)
    for index in sorted(shared):
        a, b = ref_inst.get(index), cand_inst.get(index)
        if a is None or b is None:
            out.append(Divergence(case.case_id, case.kernel, "instances.length",
                                  a, b, "placement %d has no instance" % index))
            continue
        for slot in range(INSTANCE_FLOATS):
            # Column-major, so 12..14 are the translation and are metres;
            # everything else is a direction cosine, a constant, or a light
            # level, and is compared against the dimensionless tolerance.
            tol = (DISTANCE_TOLERANCE_M if slot in (12, 13, 14)
                   else UNIT_TOLERANCE)
            if not _close(a[slot], b[slot], tol):
                out.append(Divergence(
                    case.case_id, case.kernel, "instances[%d]" % slot,
                    a[slot], b[slot], "placement %d" % index))
    return out


def _capsules_of(result: Mapping[str, Any]) -> List[Tuple[float, ...]]:
    raw = result.get("capsules") or b""
    if isinstance(raw, str):                 # an undecoded vector file
        raw = bytes.fromhex(raw)
    width = struct.calcsize(CAPSULE_PACK) * CAPSULE_FLOATS
    return [struct.unpack_from("7" + CAPSULE_PACK, raw, i * width)
            for i in range(len(raw) // width)]


def _compare_pose_capsules(case: Case, ref: Mapping[str, Any],
                           cand: Mapping[str, Any]) -> List[Divergence]:
    """**No ambiguity rules, deliberately.**

    The other three kernels each answer a *predicate* - is this inside the
    radius, did the ray strike, is this on screen - so a value sitting on the
    boundary is admissible either way. This one has no predicate: the caller
    decided which bones survive and the kernel places them. The count is
    therefore exact, and every number is a distance in metres compared against
    the one tolerance that means metres.
    """
    out: List[Divergence] = []
    ref_caps = _capsules_of(ref)
    cand_caps = _capsules_of(cand)
    if len(ref_caps) != len(cand_caps):
        out.append(Divergence(
            case.case_id, case.kernel, "capsules.count", len(ref_caps),
            len(cand_caps),
            "the caller chose the bones; the count is not a judgement call"))
        return out
    labels = ("a.x", "a.y", "a.z", "b.x", "b.y", "b.z", "radius")
    for index, (a, b) in enumerate(zip(ref_caps, cand_caps)):
        for slot in range(CAPSULE_FLOATS):
            if not _close(a[slot], b[slot]):
                out.append(Divergence(
                    case.case_id, case.kernel,
                    "capsules[%d].%s" % (index, labels[slot]),
                    a[slot], b[slot], "bone %d" % index))
    return out


_COMPARATORS = {SEGMENT_QUERY: _compare_segment_query,
                NEAREST_REGION: _compare_nearest_region,
                PLACE_BATCH: _compare_place_batch,
                POSE_CAPSULES: _compare_pose_capsules}


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

def _encode(value: Any) -> Any:
    """JSON-safe, with `bytes` tagged rather than lost.

    `place_batch` takes a lightmap and returns packed instances, and both are
    bytes at runtime - which is the point, since hex on the hot path would cost
    more than the kernel saves. So the *file format* carries the tag and the
    runtime never sees it.
    """
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if list(value) == ["__bytes__"]:
            return bytes.fromhex(value["__bytes__"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def build_vectors(seed: int = 20260909, count: int = 80) -> Dict[str, Any]:
    cases = generate_cases(seed, count)
    return {"format": "lobster-conformance/1",
            "seed": seed,
            "authority": AUTHORITY,
            "distance_tolerance_m": DISTANCE_TOLERANCE_M,
            "unit_tolerance": UNIT_TOLERANCE,
            "kernels": list(SEAM_KERNELS),
            "cases": [_encode(c.to_dict()) for c in cases],
            "expected": [_encode(r) for r in run(cases)]}


def load_vectors(path: str) -> Tuple[List[Case], List[Dict[str, Any]]]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if raw.get("format") != "lobster-conformance/1":
        raise ConformanceError(
            "{0}: not a conformance vector file (format={1!r})".format(
                path, raw.get("format")))
    return ([Case.from_dict(_decode(c)) for c in raw["cases"]],
            [_decode(r) for r in raw["expected"]])


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
