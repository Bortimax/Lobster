"""Declared constants (Scope §14).

Every number here is a *declared* budget or a *locked* dimension. L6: "Budgets
are declared and enforced per cell, not assumed for the whole world." A number
that lives in this file is one a content author can read, cite in a bug report,
and see named in a failure message.

Nothing in this module may be mutated at runtime. Per-cell overrides ride on
the Location record (`lobster_budget`), never on module state.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Locked world dimensions (§14 Budgets)
# ---------------------------------------------------------------------------

#: "Exterior cell size: 128 m x 128 m, provisional but locked as a first-class
#: constant as of v0.3 - treat as changeable with a note, not as an open
#: variable." Changing this is a scoped decision with a DECISIONS.md entry.
EXTERIOR_CELL_SIZE_M = 128.0

#: "Structure voxel resolution: ~0.25 m/voxel (proposal, unchanged)."
VOXEL_SIZE_M = 0.25

#: Terrain's own voxel size. Deliberately coarser than a structure's, and
#: deliberately a separate constant: terrain and structures are separate systems
#: (L2) and there is no reason their resolutions should be coupled. A 128 m cell
#: at 1 m/voxel is a 128x128 column grid, which a build step meshes in one pass.
TERRAIN_VOXEL_SIZE_M = 1.0

#: "Structure micro-chunk size: 8^3 voxels (proposal, unchanged)."
MICRO_CHUNK_VOXELS = 8

#: Voxels per micro-chunk.
MICRO_CHUNK_VOLUME = MICRO_CHUNK_VOXELS ** 3

# ---------------------------------------------------------------------------
# Spatial index (§7, locked to a uniform grid in v0.4)
# ---------------------------------------------------------------------------

#: "A uniform grid tuned to average entity spacing (proposal: 2.5 m, adjustable
#: per-cell)". Per-cell adjustment rides on the Location record.
SPATIAL_GRID_CELL_M = 2.5

#: 128 / 2.5 -> 52 buckets a side ("roughly a 51x51 grid" in §7).
SPATIAL_GRID_DIM = int(EXTERIOR_CELL_SIZE_M / SPATIAL_GRID_CELL_M) + 1

# ---------------------------------------------------------------------------
# Residency (§4, §3 "Don't stream - load cells")
# ---------------------------------------------------------------------------

#: "One cell (plus immediate exterior neighbors) resident at a time" (§1).
RESIDENT_RING = 1

#: Hard ceiling on simultaneously resident cells: the current cell plus a ring.
#: An exterior cell has at most 8 exterior neighbours; interiors reach their
#: neighbours through connections and are loaded one at a time.
MAX_RESIDENT_CELLS = 9

# ---------------------------------------------------------------------------
# Default per-cell budgets (§14). A cell may declare LOWER; declaring HIGHER is
# a build-step error naming the cell (see lobster.budgets).
# ---------------------------------------------------------------------------

#: Total authored structure voxels resident in one cell.
DEFAULT_MAX_STRUCTURE_VOXELS_PER_CELL = 4_000_000

#: Total micro-chunks resident in one cell.
DEFAULT_MAX_MICRO_CHUNKS_PER_CELL = 8_000

#: "Max ACTIVE-tier (full-skeleton) NPCs simultaneously resident - a function of
#: the now-locked cell size, worth a hard number next." This is that number.
DEFAULT_MAX_ACTIVE_SKELETONS_PER_CELL = 32

#: "Peak resident cells + peak memory during a transition fade: needs its own
#: ceiling and CI assertion." This is that ceiling; Scope 14 test 4 asserts it.
#: Declared first, because the per-cell default is derived from it.
MAX_TRANSITION_PEAK_BYTES = 192 * 1024 * 1024

#: Accounted bytes one cell may hold resident (terrain mesh + structures +
#: navmesh + lightmap + metadata).
#:
#: **Derived, not chosen.** It used to be a hand-picked 48 MiB, which meant the
#: three residency numbers contradicted each other: 9 resident cells x 48 MiB is
#: 432 MiB against a 192 MiB transition peak. A cell that declared nothing got a
#: ceiling it could never be allowed to actually use, and every exterior cell
#: was silently obliged to declare lower than the default - an obligation
#: nothing stated. A default that cannot compose is a lie, so it is now the
#: largest value that does. See DECISIONS.md D20.
DEFAULT_MAX_CELL_BYTES = MAX_TRANSITION_PEAK_BYTES // MAX_RESIDENT_CELLS

#: **Floor** for the broad-phase margin, not the margin itself.
#:
#: A spatial query measures to an entity's *position*, which is at its feet, so
#: the query volume has to be widened by whatever the rig occupies or hits are
#: culled before any capsule is tested. This used to *be* that number, chosen as
#: "1.8 m humanoid + margin" - which is a height argument for a radius, and
#: would have culled a 6 m wyrm one stage earlier for the same reason it culled
#: a headshot before.
#:
#: `HitTester.broad_margin()` now derives the real margin from the registered
#: rigs (`Skeleton.bound_radius`) and floors it here, so a taller or wider
#: creature raises it by itself and nobody has to remember. Both the swing and
#: the projectile broad phase use it. See DECISIONS.md D19.
PROJECTILE_BROAD_RADIUS_M = 2.5

# ---------------------------------------------------------------------------
# Hit-test frame budget - modelled, not counted (L6, DECISIONS.md D27)
# ---------------------------------------------------------------------------
#
# The previous ceilings counted broad candidates and capsule tests. Both are
# real costs and neither is the dominant one: a grid walk pays for every bucket
# key it forms and looks up, empty or not, and `buckets_visited` only ever
# counted the buckets that had somebody in them. One arrow across an empty
# 128 m cell was charged ~4 candidates for ~340 us of work.
#
# So the budget is now expressed in the unit that actually matters - time - and
# derived from counters that track it. The cost is *modelled* from counters,
# never read off a clock: a wall-clock budget would make the same frame pass on
# one machine and fail on another, and Lobster's failures have to be
# reproducible (Scope 13: "failure must be visible and attributable").

#: A 60 FPS frame.
FRAME_BUDGET_US = 16_600.0

#: One cell's hit-testing slice of that frame. Hit-testing shares the frame
#: with rendering, animation, AI, audio and physics; ~12% for one system in one
#: cell is generous and still trips while the frame is in trouble rather than
#: twelve frames later. Per cell because L6 says budgets are declared and
#: enforced per cell - a game with attacks in nine resident cells at once has a
#: different problem, and should see nine violations rather than one blurred.
HIT_TEST_FRAME_BUDGET_US = 2_000.0

# Unit costs, measured on the pure-Python path by least squares over twelve
# controlled scenarios (residual < 5 us on queries costing 18-647 us), then
# validated against real `resolve_projectile` wall time across nine densities
# and ray lengths. **Each is rounded up from its own fitted value** so the model
# over-predicts across the whole sampled range (measured ratios 1.02-1.13): a
# budget that errs must err towards tripping early, for the same reason
# `_BOUND_EPSILON` errs outward. Re-derived after D31 changed the scan count.

#: Setting a query up: normalising the ray, sampling the segment into bucket
#: coordinates, building the result list. Independent of what it finds.
#:
#: The first fit had no such term - it came back slightly negative and was
#: dropped, because at ~280 scans per query the fixed cost hid inside the scan
#: coefficient. Tightening `SpatialIndex._span` (D31) cut that to ~162 and the
#: model immediately began *under*-predicting by up to 43%, which is the wrong
#: direction for a budget. The term is explicit now, so a future change to the
#: scan count cannot quietly decalibrate the budget again.
#:
#: There is no `MAX_QUERIES_PER_FRAME` to go with it: every attack costs at
#: least one query, so such a ceiling would just be "attacks per frame", which
#: the modelled total already bounds and the violation message already reports.
PER_QUERY_US = 10.0

#: Forming a bucket key and looking it up. The traversal cost, empty or not.
PER_BUCKET_SCAN_US = 1.2

#: A candidate pulled from a bucket and distance-tested against the segment.
PER_CANDIDATE_US = 2.6

#: One counted capsule test *in situ* - not the 3.3 us primitive, but its
#: amortised share of the refinement path around it (bone matrices, region
#: selection). Measured in place, because that is what a frame actually pays.
PER_CAPSULE_TEST_US = 13.0


def modelled_cost_us(queries: int, bucket_scans: int, candidates: int,
                     capsule_tests: int) -> float:
    """What this frame's hit-testing cost, from counters alone. Deterministic."""
    return (queries * PER_QUERY_US
            + bucket_scans * PER_BUCKET_SCAN_US
            + candidates * PER_CANDIDATE_US
            + capsule_tests * PER_CAPSULE_TEST_US)


# Each unit ceiling below is the point at which that unit *alone* would spend
# the whole slice. They are secondary guards: under mixed load the frame budget
# always trips first, and it is the one to reason about. They are kept because
# "166 capsule tests spends your slice" is actionable in a way "2 ms" is not.

#: Bucket keys formed and looked up by one cell's queries in a single frame.
MAX_BUCKET_SCANS_PER_FRAME = int(HIT_TEST_FRAME_BUDGET_US / PER_BUCKET_SCAN_US)

#: Broad-phase candidates one cell may examine in a single frame.
MAX_BROAD_CANDIDATES_PER_FRAME = int(HIT_TEST_FRAME_BUDGET_US / PER_CANDIDATE_US)

#: Detailed hitbox capsule tests one cell may run in a single frame.
MAX_CAPSULE_TESTS_PER_FRAME = int(HIT_TEST_FRAME_BUDGET_US / PER_CAPSULE_TEST_US)

# ---------------------------------------------------------------------------
# Item selection budget (Scope 8, DECISIONS.md D36)
# ---------------------------------------------------------------------------

#: What one `Selector.pick` may spend. A crosshair asks "what am I looking at"
#: about once a frame, so this is ~6% of a 60 FPS frame for a once-per-frame
#: question - generous, and it has to be, because the alternative is a ceiling
#: so tight that content cannot drop loot at all.
SELECTION_PICK_BUDGET_US = 1_000.0

#: Marginal cost of one placed item in a resident cell, per pick. Measured on
#: the slope, 25 to 200 items in one cell, with a fresh frame per pick so the
#: grid build is not amortised away.
#:
#: The number has come down twice, and both times by removing work rather than
#: by choosing a friendlier figure:
#:
#: | | us/item | what changed |
#: |---|---|---|
#: | first cut | 8.3 | built a `PlacedItem`, a `Transform` and a quaternion for every item the ray missed |
#: | D36 | 4.2 | test raw records, construct nothing until something hits |
#: | D38 | **0.64** | `ItemGrid` - only items in buckets near the ray are tested |
#:
#: 0.64 is the *aimed* case, a 60 m pick. At interaction range the grid makes
#: the count stop mattering at all - 0.01 us/item, flat from 25 to 200 - and
#: the budget is derived from the worse of the two on purpose.
PER_PICKED_ITEM_US = 0.64

#: Placed items one cell may hold.
#:
#: Derived, not chosen (D20's method): a pick tests every item in **every**
#: resident cell, so the worst case is all of them at the ceiling at once.
#:
#: Item picking now has a broad phase (`ItemGrid`, D38), which is what let this
#: rise from 26. It is still derived rather than chosen: raising it further
#: means making a pick cheaper again, not editing the number.
MAX_ITEMS_PER_CELL = int(SELECTION_PICK_BUDGET_US
                         / (PER_PICKED_ITEM_US * MAX_RESIDENT_CELLS))

# ---------------------------------------------------------------------------
# Zone shape primitives - FROZEN (§4)
# ---------------------------------------------------------------------------

#: "These three primitives are frozen as of this scope... adding a fourth later
#: is a deliberate decision with its own note, not a one-line addition."
ZONE_SHAPE_PRIMITIVES = ("box", "cylinder", "polygon")

# ---------------------------------------------------------------------------
# Model kinds - FROZEN (ASSET_SCOPE §1)
# ---------------------------------------------------------------------------
#
# A model declares what kind it is; the pipeline dispatches. A scope that said
# "a model is .vox" would make every exception an `if` in the loader, and the
# project owner asked for exceptions. This is the same shape as the render
# backend chain (D29) and the accelerator kernels (D40): a declared set, a
# chooser that reports which it picked, every implementation held to the same
# tests.

#: `voxel` - a MagicaVoxel `.vox` file, named by `Model.asset_ref`. The default,
#: and the Scope's own choice (§4.5).
MODEL_VOXEL = "voxel"

#: `primitive` - a shape and dimensions declared in the record itself. No file,
#: no importer, no art tool. Removes the *need* for most exceptions rather than
#: being one.
MODEL_PRIMITIVE = "primitive"

#: Frozen at two, for the reason `ZONE_SHAPE_PRIMITIVES` is frozen at three:
#: adding a third is a decision with its own DECISIONS entry, not a one-line
#: addition. Sprites and conventional meshes were both considered and declined
#: with reasons (ASSET_SCOPE §1).
MODEL_KINDS = (MODEL_VOXEL, MODEL_PRIMITIVE)

#: The shapes a `primitive` model may be. Frozen on the same argument: three
#: cover the cases that are genuinely geometric, and a fourth is a decision.
#: Anything wanting a *file* is the mesh importer wearing a primitive's
#: clothes, which ASSET_SCOPE §1 refuses in writing.
PRIMITIVE_SHAPES = ("box", "cylinder", "quad")

# ---------------------------------------------------------------------------
# Bundle format
# ---------------------------------------------------------------------------

BUNDLE_FORMAT = "lobster-cell"
BUNDLE_FORMAT_VERSION = 1
BUNDLE_SUFFIX = ".lobster_cell"

#: Package id under which Lobster declares its Octopus schema extension.
GEOMETRY_PACKAGE_ID = "lobster.geometry"
LIMB_STATE_PACKAGE_ID = "lobster.limb_state"
