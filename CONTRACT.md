# The cheap contract

> The cheap contract (§13) is part of the delivery surface, not optional
> documentation.

This file is what Shrimp and the authoring tool bind against. Everything in it
is asserted by the suite — `tests/test_contract.py` for the event, query and
record surface, and `test_render.py`, `test_placement.py`, `test_gating_volumes.py`
and `test_cell_budget.py` for the rest. If a test and this file ever disagree,
the test is right and this file is stale.

`python -m lobster.cli contract` prints the machine-readable surface as JSON, so
a tool can read it rather than parse this.

> **A note on section numbers.** `§N` in this document always refers to
> **`LOBSTER_SCOPE.md`**. This document's own sections are numbered `1.`–`9.`
> and are referred to as "CONTRACT §N". The two are not the same numbering and
> never line up.

---

## What you are binding against

**Pure Python, standard library only, and that is the reference implementation —
not a placeholder for one.** Every rule in this document is executable: you can
open `lobster/`, read how a capsule test or a micro-chunk mesh actually works,
and change it.

**The hottest loops are replaceable by an optional native module** — broad-phase
spatial queries and capsule refinement, at *volley* granularity. When it is
present it is used; when it is absent the pure-Python path runs and the whole
suite still passes. `python -m lobster.cli contract` reports which paths are live
here, the same way it reports which render backends can draw.

**The honest cost:** on the fast path, reading the Python still tells you the
truth about semantics, but editing it does not change what executes. Both paths
are held to the same answers by differential tests, with a declared float
tolerance and a named authority where they differ. See DECISIONS.md D26.

**Neither native path is written yet.** The GPU backend (D22) and the arithmetic
accelerator (D26) both name their chosen answer and both report themselves as
unimplemented rather than pretending. Today Lobster runs pure Python everywhere,
and it is fast enough for tools, tests and offline builds — not for a 200-arrow
volley at 60 FPS. That number is measured and recorded in D26 rather than
estimated.

---

## 1. Events

Seven, exactly. `lobster.events.CONTRACT_EVENTS` is the closed set — subscribing
to anything else raises `ContractViolation`, and so does emitting a payload of
the wrong type.

**Five of them Lobster originates. Two it declares but does not yet raise
itself** — the column says which, and `tests/test_contract.py` pins it, so the
split cannot drift silently.

| Event | Payload fields | Raised by |
|---|---|---|
| `on_enter_cell` | `location_id: str` | **Lobster** (`CellManager.load`) |
| `on_exit_cell` | `location_id: str` | **Lobster** (`CellManager.unload`) |
| `on_hit_location` | `target_id: str`, `region: str \| None`, `force: float`, `source_id: str \| None` | **Lobster** (`HitTester`, `WorldHitTester`) |
| `on_structure_damaged` | `structure_id: str`, `chunk_indices: list[int]` | **Lobster** (`CellManager.damage_structure`, `StructureStateWriter`) |
| `on_interact` | `target_id: str` | **Lobster** (`Selector.interact`) |
| `on_item_placed` | `item_id: str`, `cell_id: str`, `transform: Transform` | *caller* — awaits Scope §8 |
| `on_item_removed` | `item_id: str`, `cell_id: str`, `transform: Transform` | *caller* — awaits Scope §8 |

The last two are fully wired — payload types, `EventBus.item_placed()` /
`.item_removed()` — but nothing originates them. Scope §8 gives Lobster three
things: *object selection/raycasting* (**built**, see CONTRACT §8), *world-space labels*,
and *physically placing/removing an item's representation in the world*. The
second and third are not built, and those two Events are the visible end of the
third.

**So today: if you place an item in the world, fire `on_item_placed` yourself.**
See DECISIONS.md D24 and D25.

Every payload is a frozen dataclass with `.to_dict()`, which is the wire form:

```json
{"event": "on_hit_location", "target_id": "npc-ada", "region": "left_arm",
 "force": 12.5, "source_id": "player"}

{"event": "on_structure_damaged", "structure_id": "keep-gatehouse",
 "chunk_indices": [12, 13, 20]}

{"event": "on_item_placed", "item_id": "item-sword", "cell_id": "cell-village",
 "transform": {"position": [4.0, 0.0, 2.0], "rotation": [0.0, 0.0, 0.0, 1.0]}}
```

`chunk_indices` is sorted and deduplicated. A `Transform` is always
`{"position": [x, y, z], "rotation": [x, y, z, w]}`, metres, right-handed, Y up.

### `region` — resolved at every tier

A hit names one of the six regions whenever the target has a rig, at **both**
ACTIVE and PROJECTILE tier. A sniper's shot two cells away resolves to a limb
exactly as a sword blow does. `region` is `null` only when the target has no rig
at all. See DECISIONS.md D16, which supersedes D5.

The six regions are frozen: `head`, `torso`, `left_arm`, `right_arm`,
`left_leg`, `right_leg`. Non-humanoid creatures declare their own `RegionSet` at
content time; the mechanism stays Lobster's.

How it resolves, by tier:

| Tier | Decides *if* it hit | Decides *where* |
|---|---|---|
| ACTIVE | per-bone capsules | same test — no bone intersected is a miss |
| PROJECTILE | one whole-body capsule | per-bone capsules, only on a landed hit |
| DORMANT | no hitbox — §6.5's occupancy + Event path | — |

Gating the refinement behind the body capsule is what keeps this affordable: a
**miss costs one capsule test**, and only a landed hit pays for the six. Measured
at 1.00 refinements per landed hit.

**Every gating volume is derived from the rig, and this matters for non-humanoids.**
The whole-body capsule bounds the rig it stands for — axis on the rig's Y span,
radius from how far any bone reaches off that axis — so a wide creature is
shootable at range, not melee-only by accident. The broad phase is widened by
`Skeleton.bound_radius()`, derived per rig and floored at
`PROJECTILE_BROAD_RADIUS_M`, so a tall creature is not culled before any capsule
is tested. Ship whatever `RegionSet` a creature needs; nothing assumes a
humanoid. See DECISIONS.md D19.

`HitResult` (Lobster's return type, not the wire event) carries the honesty:

| Field | Meaning |
|---|---|
| `region_precise` | `True` — the ray intersected that bone. `False` — the body capsule was hit but no bone was, so `region` is the *nearest* bone (a graze, or a stale pose on a distant target). A blast is always `False`: no ray to trace, so the region is the one nearest the epicentre. |
| `pose_version` | how fresh the pose that resolved the region was — the counterpart to `snapshot_seq` for position |

Both are measurements, not guesses: every capsule is tested and the minimum
taken. Neither appears on `on_hit_location`, whose four fields are fixed by §13.

**The limb-state read applies at every tier.** A severed limb offers no hitbox to
a sniper any more than to a swordsman (§5 sets no tier condition on it).

### The per-frame budget

Hit-testing is budgeted like everything else (L6). Call `HitTester.begin_frame()`
each frame; a frame that costs more than its slice raises `BudgetViolation`
naming the cell, the breakdown, and **the driver**.

**The budget is time, modelled from counters — never read off a clock.** A
wall-clock budget would pass on one machine and fail on another; the same frame
must fail identically everywhere, because §13 requires failure to be visible and
attributable, and that includes reproducible.

| | |
|---|---|
| `HIT_TEST_FRAME_BUDGET_US` | **2,000 µs** — one cell's slice of a 16.6 ms frame |
| `PER_BUCKET_SCAN_US` | 1.2 — forming a bucket key and looking it up |
| `PER_CANDIDATE_US` | 2.5 — a candidate pulled and distance-tested |
| `PER_CAPSULE_TEST_US` | 12.0 — a counted capsule test *in situ*, refinement path included |

`modelled_cost_us(scans, candidates, capsules)` is the whole model. The unit
costs are measured, not guessed, and rounded **up** from the fit so the model
over-predicts across the sampled range — a budget that errs must err toward
tripping early.

Three unit ceilings are derived from the same slice and enforced as secondary
guards. Each is the point at which that unit **alone** would spend it, so under
mixed load the frame budget always fires first:

| Ceiling | Derived |
|---|---|
| `MAX_BUCKET_SCANS_PER_FRAME` | 1,666 |
| `MAX_BROAD_CANDIDATES_PER_FRAME` | 800 |
| `MAX_CAPSULE_TESTS_PER_FRAME` | 166 |

Pass `0` for any of them, or `frame_budget_us=0`, to disable that check (tools,
offline analysis, tests).

**What this buys on the pure-Python path: about four 120 m arrows into a
200-strong army, per cell, per frame.** That number is small and it is true. The
previous ceiling permitted roughly 425 — about 196 ms of wall time, twelve
frames past the point the game stopped being one. Raising the budget is not the
remedy; the accelerator seam (DECISIONS.md D26) is.

### The violation names the driver, because the remedy depends on it

Two engagements blow the same budget for opposite reasons:

| Driver | Shape | What the message says |
|---|---|---|
| **broad candidates** | a packed crowd | mass-casualty event — use the zone-occupancy path (§6.5/§7), resolved once |
| **grid traversal** | long shots over open ground | grid-walk cost, scaling with ray length × queries and *not* with bodies near the path — shorten the segment resolved per frame, or use the volley seam |

Telling the second caller to use zone occupancy would be nonsense advice: there
is no crowd. See DECISIONS.md D27.

### Subscribing

```python
from lobster.events import EventBus, ON_HIT_LOCATION

bus = EventBus()
bus.subscribe(ON_HIT_LOCATION, lambda e: print(e.to_dict()))
bus.subscribe_all(my_recorder)          # every contract event
```

`OctopusEventSink(session).attach(bus)` forwards the subset Octopus has trigger
types for, as bindings (record ids only — a list has no place in a binding).
`on_enter_cell` is **not** forwarded: loading a neighbouring cell for residency
is not the player entering a scene. Call `sink.enter_scene(location_id)` when the
player actually arrives.

---

## 2. The one record the save integration depends on

```
StructureState (Octopus record, declared by packages/lobster_geometry.json)
  id:               matches the authored structure_id
  location_id:      REPLACE
  destroyed_chunks: UNION_TOMBSTONED, Set<u16> of micro-chunk indices
```

`UNION_TOMBSTONED` because destruction is additive and monotonic: two mods
damaging different walls of the same keep both apply. Repair is
`DELETE_ENTRY`, one per index.

`destroyed_chunks` is **not** save-layer-only, so a mod may ship a pre-ruined
keep (DECISIONS.md D3).

Write it through `lobster.structure_state`:

```python
writer = StructureStateWriter(session, bus)
writer.destroy("keep-gatehouse", [12, 13])   # MERGE + on_structure_damaged
writer.repair("keep-gatehouse", [12])        # DELETE_ENTRY
```

or take the operations as plain data and write them yourself:

```python
destroy_op("keep-gatehouse", [13, 12])
# {"op": "MERGE", "id": "keep-gatehouse", "field": "destroyed_chunks",
#  "values": [12, 13]}
```

### Damaging many structures at once

For an offscreen siege — a town that is not resident gets a computed outcome and
every building the raid overran needs updating in one pass:

```python
result = writer.destroy_many([("bld-mill", [4, 5]),
                              ("bld-barn", [0]),
                              ("keep-gatehouse", [12, 13])])

result.written            # {"bld-mill": (4, 5), ...} - what was actually written
result.skipped            # chunks already destroyed, per structure
result.chunks_destroyed() # 5
```

No resident cell is needed; the writer only ever holds a session.

Three guarantees, and they are the reason to prefer this over a loop:

1. **Atomic.** Every entry is validated before any op is written. A siege naming
   one structure that does not resolve raises having written *nothing* — rather
   than leaving the town half-sacked with some ops committed and some Events
   already fired.
2. **No redundant writes.** Chunks already destroyed are skipped and reported in
   `skipped`. Re-running the same siege ten times appends five operations, not
   fifty. Pass `skip_redundant=False` if you have already diffed against the
   world yourself.
3. **Coalesced.** Two entries naming the same structure merge into one operation
   and one Event.

**Events are still one `on_structure_damaged` per structure.** §13 fixes that
shape and there is no batch Event; count them if you want one notification per
siege.

Validation covers what a writer can see — the structure resolves, and the
indices are non-negative integers. It deliberately does *not* check them against
`grid_size`, which needs the authored bundle: a stale index is handled at load
time by quarantining that index with a reason (§6, v0.4).

**On speed, honestly:** this is not much of a speed fix. A single `destroy`
costs ~7 µs and 200 of them cost 1.5 ms, because Octopus's resolution is lazy
and incremental — the writes queue and one re-resolution settles them all. A
batch of 200 costs 1.3 ms. The win is the three guarantees above, not the clock.

The one thing that *is* 4× slower is reading the resolution between writes:

```python
for structure_id, chunks in siege:          # 19 ms for 200 structures
    writer.destroy(structure_id, chunks)
    session.resolution()                    # <- this is the cost

writer.destroy_many(siege)                  # 1.3 ms, and atomic
```

**Out-of-bounds indices quarantine, they do not crash.** A save built against a
larger `grid_size` keeps every valid index and logs the stale ones with a reason
(`ResidentCell.quarantined`, `CellManager.quarantined()`).

---

## 3. Live queries Lobster is permitted to call

All pure, never cached beyond the current hit-test or frame. The set is closed
(`lobster.octopus_bridge.PERMITTED_QUERIES`):

| Query | Today |
|---|---|
| `resolve_npc_state` | Octopus's own (`lce.npc`) |
| `zone_occupants` | Octopus's own (`lce.zones`) |
| `resolve_equipment_slots` | Octopus's own (`lce.queries`) |
| `list_active_effects` | Octopus's own (`lce.queries`) |
| `limb_state` | reads `Character.limb_state`; hit-testing only |
| `structures_in_zone` | Lobster, pending Octopus (§13 is new in v0.3) |
| `structures_in_location` | Lobster, pending Octopus |

`delegates_to_octopus()` reports which is which, so the day Octopus lands the
last two the swap is one function and a green test.

They are reached through a `FrameView`, which closes at the end of the frame:

```python
bridge = OctopusBridge(session)
with bridge.frame() as view:
    occupants = view.zone_occupants("zone-market")
    structures = view.structures_in_location("cell-village")
# view is now closed; calling it again raises ContractError
```

`FrameView.record`, `records_of_type`, `connections` and `connection_path` are
record reads rather than Scope §13 queries, and are not on the list — see
DECISIONS.md D9.

### The limb-state read

```python
from lobster.octopus_bridge import HIT_TEST_ONLY

states = view.limb_state("npc-ada", purpose=HIT_TEST_ONLY)
```

`purpose` is mandatory and must be the sentinel. There is exactly one caller in
Lobster (`lobster/hittest.py`), and a test asserts it stays that way.

**Shrimp reads the same field, independently.** Not through this — through its
own query, in its own repository, and it makes its own decision about what to
pose. Both systems reading the same source of truth separately is the correct
boundary; Lobster reading it once and pushing a decision downstream is not
(§5, L8).

### Tier discipline

**A tier is not an entity class.** It is not an archetype, an LOD level, a
creature type, or anything content authors declare. It is *per-entity,
per-cell-residency runtime state*, set by the game when it places an entity, and
it answers exactly one question:

> How precisely can this entity be hit **right now**?

Nothing in Lobster stores a tier on a record, an archetype, or a creature.
`cell.place(entity_id, position, tier)` takes it as an argument, `set_tier`
changes it mid-residency, and a fresh residency starts over. There is no such
thing as "a PROJECTILE-tier creature" — there is an entity that is, at this
moment, a candidate for a projectile hit-test.

| Tier | Means | Costs |
|---|---|---|
| `ACTIVE` | in the current cell and in melee reach | a rig, full 6-region tests |
| `PROJECTILE` | within the range of a projectile **currently being resolved**, whatever cell it is in | a rig, one body-capsule test per shot |
| `DORMANT` | present in the world, **not a hit-test candidate** | nothing — no rig, no tests |

`PROJECTILE` is scoped by *the attack in flight*, not by distance to the player.
A fireball two connections away makes distant NPCs candidates for that one test;
they are not "in PROJECTILE mode" before it is cast or after it lands.

**`DORMANT` is the cheap tier, and it is the right default for a mob nobody is
fighting.** It needs no `Skeleton` registered and costs nothing per frame.
Effects still reach a DORMANT entity — through §6.5's occupancy + Event path,
which is how an offscreen siege kills people. If you want a lightweight mob,
this is the tier, not `PROJECTILE`.

Placing an entity at `ACTIVE` or `PROJECTILE` **with no rig registered raises**
`HitTestError` naming the entity and both fixes. It used to be silently
unhittable — arrows passed straight through with no error anywhere — which is
the worst way for an integration mistake to present (§13 invariant 2).

### A shot that crosses the cell boundary

`HitTester` resolves against **one** cell. For a projectile that may leave the
cell it was fired from — which is what PROJECTILE means — use `WorldHitTester`:

```python
from lobster.hittest import WorldHitTester

tester = WorldHitTester.from_manager(manager, view, bus)
tester.begin_frame()
hits = tester.resolve_projectile(view, origin, direction, 45.0, force,
                                 source_id="player")
```

`origin` and `direction` are **world space**. Results come back **nearest first**,
each carrying `cell_id` (which cell the target was in) and `distance_from_source`
(metres from the shooter). With `first_hit_only=True` you get the single nearest
hit across every cell — decided after all of them have answered, so a shot
cannot pass through a near target to reach a far one in a different cell.

Only hits that are **returned** fire `on_hit_location`. A candidate the shot
passed on its way to a nearer target is never reported.

Everything else is unchanged and deliberately so:

| | |
|---|---|
| **Melee stays single-cell** | ACTIVE is "current cell + immediate melee range"; a sword does not reach the next cell. `WorldHitTester` has no `resolve_swing`. |
| **Blasts stay as they were** | Scope §6.5 already resolves mass-casualty through zone occupancy, which is Octopus records and cell-agnostic. |
| **No tier is promoted** | being shot from another cell wakes nobody; `octopus_tier_for(PROJECTILE)` is still `None`. |
| **Snapshot provenance survives** | a cross-cell hit reports the *target cell's* `snapshot_seq` and `snapshot_reason`, not the shooter's. |

Cells the shot cannot reach are rejected before any sweep, so this costs the
same as the single-cell path plus one box test per resident cell. See
DECISIONS.md D23.

### Moving an entity

| Situation | Call |
|---|---|
| an ACTIVE entity being simulated | `index.move(entity_id, position)` |
| a scripted relocation, a mod effect, anything moving a non-ACTIVE entity | `index.refresh_snapshot(entity_id, position)` |
| a tier change | `index.set_tier(entity_id, tier)` |

`move` **refuses** a non-ACTIVE entity and says so. That is deliberate: a
DORMANT or PROJECTILE entity's position is a *snapshot* taken on cell entry and
exit, not a live value, so a caller with a new position for one got it from
somewhere other than simulation and needs to stamp the provenance.
`refresh_snapshot` does that, and every hit result then reports the
`snapshot_seq` and `snapshot_reason` it resolved against.

Skipping this is how PROJECTILE-tier queries silently go stale — Scope §15.10
names it as the failure mode reopened from a different angle, and the refusal is
what keeps it closed.

`ACTIVE` and `DORMANT` are the same concept as Octopus's and share its spelling
(checked against `lce.npc` at import). `PROJECTILE` is Lobster's own and is
**never** passed to Octopus: `octopus_tier_for(PROJECTILE)` is `None`, which
Octopus treats as not-ACTIVE. Being shot at from two cells away does not promote
an NPC into combat simulation.

---

## 4. The `set_pose` interface

Lobster owns pose data. Shrimp's animation controller owns what pose to ask for.

```python
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.geometry import Transform

skeleton = Skeleton("npc-ada", humanoid_region_set(height=1.8),
                    root=Transform(position=(10.0, 0.0, 4.0)))

skeleton.set_pose({"arm_l": Transform(rotation=(0.0, 0.0, 0.38, 0.92))},
                  root=Transform(position=(10.5, 0.0, 4.0)))
```

- `pose` maps bone id → **entity-local** `Transform`. Bones left out keep their
  current transform, so posing a subset is fine. An unknown bone id raises.
- `root` optionally moves the entity; `set_root` does it alone.
- `pose_version` increments on every write, so a consumer can detect a stale
  read.

Reading back:

| Call | Returns |
|---|---|
| `skeleton.pose()` | `{bone_id: Transform}`, entity-local |
| `skeleton.bone_matrices()` | `{bone_id: Transform}`, world space — what a renderer and IK consume |
| `skeleton.capsule_for(bone_id)` | world-space `Capsule` |
| `skeleton.hitboxes(limb_state=...)` | `[(region, Capsule)]`, severed limbs omitted |
| `skeleton.whole_body_capsule()` | the single PROJECTILE-tier capsule |

`hitboxes` takes the limb map **as an argument**. Nothing in `lobster/skeleton.py`
reads it from Octopus, and it cannot: producing one requires a `FrameView` and
the `HIT_TEST_ONLY` sentinel.

There is no `play`, no `blend`, no `transition`, no `on_hit`, and no ragdoll
decision. `set_pose` and `set_root` are the only mutators, and a test asserts
that too (L8).

### Procedural IK

`lobster.ik` runs *after* a pose is set:

```python
result = solve_foot_placement(skeleton, terrain_ground_fn(cell.terrain))
apply(skeleton, result)      # separate step, deliberately
```

Solving mutates nothing. Whether to apply the correction — blend it in, skip it
while ragdolling — is an animation decision and therefore Shrimp's.

---

## 5. Declared invariants

1. **Geometry is data.** No private save format anywhere, including a mod's
   sound-source list. `.lobster_cell` is a derived build artifact, deletable and
   rebuildable; everything mutable is an Octopus record or field. The runtime
   package has no file-write path at all, and a test asserts it.
2. **Failure is visible and attributable.** Over-budget content raises
   `BudgetViolation` naming the specific record and the specific cell. Build
   findings carry `record_id` and `cell_id`. Quarantined break-state indices
   carry the reason.
3. **No hidden coupling to a specific game's content.** Nothing in `lobster/`
   names a location, a creature, a faction or an item. The only content Lobster
   ships is two schema-extension packages that declare fields.

---

## 6. Budgets

Declared in `lobster/constants.py`, overridable **downward** per cell via
`Location.lobster_budget`:

| Constant | Value |
|---|---|
| `EXTERIOR_CELL_SIZE_M` | 128.0 (locked, §14) |
| `VOXEL_SIZE_M` | 0.25 (structures) |
| `TERRAIN_VOXEL_SIZE_M` | 1.0 (D12) |
| `MICRO_CHUNK_VOXELS` | 8 |
| `SPATIAL_GRID_CELL_M` | 2.5 (uniform grid, locked in v0.4) |
| `MAX_RESIDENT_CELLS` | 9 |
| `DEFAULT_MAX_STRUCTURE_VOXELS_PER_CELL` | 4,000,000 |
| `DEFAULT_MAX_MICRO_CHUNKS_PER_CELL` | 8,000 |
| `DEFAULT_MAX_ACTIVE_SKELETONS_PER_CELL` | 32 |
| `MAX_TRANSITION_PEAK_BYTES` | 192 MiB |
| `DEFAULT_MAX_CELL_BYTES` | **21.3 MiB — derived**, `MAX_TRANSITION_PEAK_BYTES // MAX_RESIDENT_CELLS` |
| `PROJECTILE_BROAD_RADIUS_M` | 2.5 — a *floor*; the real margin is derived per rig |

A cell declaring a *higher* ceiling than the shell default is itself a
violation. `python -m lobster.cli budgets --cells DIR` prints declared versus
actual per cell.

### A cell's budget must leave room for its ring, not just for itself

This is the one that bites. A transition holds the **union of both residency
sets** — it loads before it unloads (§14 test 4) — so a per-cell ceiling only
means something next to the cells it will be resident *alongside*.

`DEFAULT_MAX_CELL_BYTES` is derived from the peak for exactly that reason: a
hand-picked 48 MiB meant 9 × 48 = 432 MiB against a 192 MiB peak, so a cell that
declared nothing got a ceiling it could never be allowed to use, and every
exterior cell was silently obliged to declare lower. Deriving it makes a full
residency of all-default cells fit by construction.

If you declare your own, check the composition:

```bash
python -m lobster.cli budgets --cells cells --packages world.json
```

reports `over_transition_peak` findings — for each cell, the worst transition
out of it, the cells that union holds, the declared total, and the *actual*
baked total beside it. A cell may declare 21 MiB and use 3; only the second
number says whether a transition will really fail. Exit status is non-zero if
any cell is over, either individually or composed.

An exterior cell in a 4-connected ring is resident with 4 neighbours, and a walk
between two of them unions to 8 — so budget for eight-way company, not for one.

---

## 7. Rendering

Lobster draws. The geometry half is `lobster.camera` (frustum, projection) and
`lobster.visibility` (cell-boundary culling, draw list); the drawing half is a
**backend**.

```python
from lobster.camera import Camera
from lobster.render import RenderSettings, render_resident, select_backend

camera = Camera.looking_at(eye, target)
frame = render_resident(camera, manager, view,
                        settings=RenderSettings(width=1280, height=720))
```

### Backends — three tiers

| tier | what it is | what to expect |
|---|---|---|
| `moderngl` | OpenGL 3.3+ on a real GPU | **the default and the target.** 60 FPS on a 128 m cell with destructible geometry, and the battery-efficient path on a phone |
| `moderngl-llvmpipe` | the same GL code on Mesa's software rasteriser | **fast enough for development and many tests.** Not player-facing — do not benchmark against it |
| `software` | the pure-Python rasteriser | correct pixels for a screenshot, a debug overlay, or a CI oracle. Slow, and viable everywhere |

The top two are the same code: llvmpipe is a *driver*, not a renderer. They are
reported as separate tiers because their performance is two orders of magnitude
apart and a caller who landed on the middle one needs to know.

**The middle tier is optional and never vendored.** Mesa is an OS package; when
it is absent the chain drops to pure Python without drama. **The bottom tier
stays viable on purpose** — some CI images have Mesa and some do not, so the
zero-external-package path must keep working or CI becomes environment-dependent.

### Selection reports the whole chain

Silent fallback is how somebody spends an afternoon profiling the wrong layer,
so selection hands back the story with the answer:

```python
backend = select_backend()
log.info("render backend: %s", backend.selection.summary())
# render backend: moderngl unavailable -> moderngl-llvmpipe unavailable -> software
```

`backend.selection` is a `SelectionReport` (`chosen`, `skipped` as
`(name, reason)` pairs, `summary()`). `selection_report()` returns it without
constructing anything; `probe()` returns per-tier `BackendInfo`; **every tier
that cannot run says why, always.** `python -m lobster.cli contract` prints both
under `render_backends` and `render_backend_selected`.

`select_backend("moderngl")` **raises** if that tier cannot run, rather than
falling back — a caller who asked for the GPU and quietly got a Python
rasteriser would draw the right picture and blame the wrong thing for the frame
rate.

Core Lobster has **no hard graphics dependency**: every test runs on a machine
with no GPU, no display and no driver. See DECISIONS.md D22 and D29.

### Where cells are

A connection says where you *arrive*, not where a cell *sits*. Exterior cells
declare their own placement:

```json
{"id": "cell-wild-3-4", "type": "Location", "tags": ["exterior"],
 "exterior_grid": [3, 4]}
```

placed at `[x * 128, 0, z * 128]` using the locked cell size. **Interiors declare
nothing** — an interior is its own coordinate space, and that is what makes the
cell model cheap.

Without this an exterior world stops at the cell boundary: there is no offset to
draw the neighbour at. The build step enforces it — `exterior_without_grid`,
`exterior_grid_collision` and `exterior_grid_malformed` fail a build;
`exterior_grid_not_adjacent` and `exterior_terrain_undersized` warn. See
DECISIONS.md D21.

Residency still comes from `connections`, not from the grid (D8). The grid says
where cells *are*; the connection graph says which are *loaded*.

`CellManager.placements(view)` returns `{cell_id: Transform}` for everything
currently resident — that is what `render_resident` passes to the culler, and
what you pass yourself if you drive `build_draw_list` directly. `lobster.visibility`
reads no records; it only ever uses placements it is handed.

### Sampling the baked lightmap

```python
level = cell.ambient_at((12.0, 2.0, 30.0))   # 0..1
```

Scope §3: *"structures sample ambient at their position. No per-structure light
grid to store or update on damage."* That is why destroying half a keep costs no
relighting — the surviving chunks sample this, at their position, exactly as they
did before. A cell with no baked lightmap returns `1.0`, so an unbaked or
hand-made cell renders lit rather than black.

---

## 8. Object selection

*Scope §8 item 1, DECISIONS.md D25.* "What am I pointing at?"

```python
from lobster.selection import Selector, ENTITY, STRUCTURE, PROP, TERRAIN

selector = Selector.from_manager(manager, view)
found = selector.pick(camera.position, camera.forward, 50.0)
```

`pick` returns the nearest `Selection` or `None`. `pick_all` returns every hit
along the ray, nearest first. Both take `kinds=` to narrow the search and an
optional `view=` (see below).

| `Selection` field | Meaning |
|---|---|
| `kind` | one of `"entity"`, `"structure"`, `"prop"`, `"terrain"` — the closed set `SELECTION_KINDS`; anything else raises `SelectionError` |
| `target_id` | the entity, structure or prop id. For terrain it is the **cell id**, because a cell's ground has no other name |
| `cell_id` | which resident cell the thing lives in |
| `point` | where the ray met the surface, **world space** — already through the cell placement |
| `normal` | surface normal at that point, world space. `(0, 0, 0)` where none was measured (props) |
| `distance` | metres from the ray origin |
| `chunk_index` | structures only — **the same index `damage_structure` takes**, so a pick becomes a hit without a second query |
| `region` | entities only — one of the rig's regions |

`.to_dict()` is the wire form, same as an Event payload.

### It is not hit-testing

They share the ray maths and nothing else, and merging them would be a defect:

| | Hit-test (`HitTester`) | Selection (`Selector`) |
|---|---|---|
| asks | does this attack connect, how hard, where | what is under this ray |
| considers | rigged bodies, filtered by tier | entities, structures, props, terrain |
| reports | `force`, an `on_hit_location` Event | a point, a normal, a distance |
| budget | `begin_frame()` ceilings apply | none — one ray, on demand |

A DORMANT entity has no rig, so a hit-test **raises** (`HitTestError`: an attack
that cannot resolve is an integration bug) while a pick simply returns `None`
(pointing at empty air is not).

### What it refuses to decide

There is no `is_interactable`, no "usable" filter, and no ordering by
importance. **Nearest wins** — the only ordering geometry supports. Whether the
barrel opens, whether the NPC will talk, whether the door is locked: all of that
is content's (L4). Lobster reports what is there.

`Selection` and `Selector` are asserted not to grow such an attribute
(`tests/test_selection.py`).

### Break state and limb state are both respected

A pick has to agree with what is on screen:

- The structure march reads `LiveStructure.is_solid`, so a destroyed micro-chunk
  is **not** pickable — flatten a gatehouse and you point straight through it.
- Pass `view=` and a severed limb offers no target, exactly as in a hit-test
  (§5). Without a `view` every rig region is pickable, which is the right
  default for a tool or a test. `lobster/selection.py` is the second and last
  permitted reader of `limb_state`, and it asks the identical question:
  *which capsules exist right now.*

### Cross-cell

Selection spans the whole resident set from the start. The ray is transformed
into each cell's space (`placement.inverse_apply`), exactly as the cross-cell
shot does it (CONTRACT §3, D23) — the ray moves, the world does not. `point` comes back
in world space. Terrain is claimed by the cell whose footprint actually covers
the column (`TerrainCollider.covers`), not by whichever cell answered first.

### `interact`

```python
found = selector.interact(bus, origin, direction, 3.0, view=view)
```

Picks, then raises `on_interact(target_id)` for what it found — nothing if it
found nothing. **This is the only place Lobster originates that Event**, and it
closes the gap D24 recorded.

The *decision* that an interaction happened stays outside: only the consumer
knows a button was pressed. Terrain is excluded by default, because
`on_interact(target_id)` cannot usefully say "the ground" — the id would be the
cell. Pass `kinds=` to include it.

---

## 9. Zone trigger volumes

A Zone becomes a trigger volume through two fields, both declared by
`packages/lobster_geometry.json`:

| Field | Merge policy | Meaning |
|---|---|---|
| `Zone.shape` | `REPLACE` | the volume itself; `null` means the Zone has no geometry |
| `Zone.shape_location_ref` | `REPLACE` | which **cell's coordinate space** `shape` is expressed in |

A Zone with no `shape` is perfectly legal and common — Octopus's Zone is a query
index first, and only a Zone somebody wants to *stand in* needs geometry.

### The primitives — frozen

`box`, `cylinder`, `polygon`. Nothing else parses; `shape_from_dict` raises
rather than falling back to a bounding box, and the build-step lint rejects a
fourth by name (`unknown_zone_shape`). Adding one is a Scope change with its own
DECISIONS.md entry.

```json
{"kind": "box",      "center": [64, 2, 64], "size": [128, 8, 128]}
{"kind": "cylinder", "center": [10, 0, 10], "radius": 6, "height": 4}
{"kind": "polygon",  "points": [[0,0],[8,0],[8,6],[0,6]], "min_y": 0, "max_y": 4}
```

Coordinates are metres in **cell space**, Y up. Polygon `points` are `[x, z]`
pairs on the ground plane, extruded between `min_y` and `max_y`.

### `shape_location_ref` — which cell the coordinates mean

Octopus has no geometry and a `Zone` may span several `Location`s, so `[64, 2,
64]` is meaningless until you know *whose* origin it is measured from. That is
the only job this field has. `volumes_for_cell(view, cell_id)` resolves it:

| `shape_location_ref` | Result |
|---|---|
| set to a Location id | the volume exists **only** in that cell, and nowhere else |
| `null` (default) | the volume is offered to **every** cell in `Zone.location_refs`, each interpreting the coordinates in its own local space |

**Author this field.** The `null` fallback exists so a single-Location Zone works
with no ceremony, but on a Zone spanning three Locations it means the same box
appears at the same local coordinates in all three cells — which is almost never
what a market-square trigger wants. Set `shape_location_ref` to the cell the
volume was actually laid out in.

```json
{"id": "zone-market", "type": "Zone",
 "location_refs": ["cell-village", "cell-keep"],
 "shape_location_ref": "cell-village",
 "shape": {"kind": "cylinder", "center": [40, 0, 52], "radius": 12,
           "height": 6}}
```

Occupancy is a separate question and not this one: `zones_containing(volumes,
point)` answers *"is this point inside that volume"*, and
`queries.zone_occupants` answers *"who is in this Zone"*. Lobster does not
invent the second (§4). See DECISIONS.md D14.

---

## 10. Build-step lint

`lobster-build` refuses to bake a world that cannot work. Every finding carries
`code`, `record_id` and `cell_id`; the codes in `ERROR_CODES` fail the build and
nothing is written at all, rather than a partial bake hiding the problem behind
a runtime symptom weeks later (§15.2).

| Code | Fails build | What it catches |
|---|---|---|
| `one_way_connection` | yes | a connection with no return edge — Octopus's own `lce.lint` (its D26), reused rather than re-implemented |
| `unenterable_location` | yes | **a Location with no `default_spawn_transform` and no incoming connection carrying a `spawn_transform`** (§4) |
| `sound_bypass_channel` | yes | a manifest trying to bake record-owned data (sound lists, zone shapes, spawn transforms) into the bundle (§4.5) |
| `unknown_zone_shape` | yes | a fourth zone primitive |
| `structure_location_mismatch` | yes | `StructureState.location_id` disagrees with the cell the geometry is baked into |
| `structure_without_geometry` | yes | a `StructureState` naming a cell that bakes nothing for it |
| `override_out_of_range` | yes | a `navmesh_load_bearing` override on a chunk index that no longer exists |
| `exterior_without_grid` | yes | a connected exterior cell with no `exterior_grid`, so nothing knows where to draw it |
| `exterior_grid_collision` | yes | two exterior cells claiming the same grid square |
| `exterior_grid_malformed` | yes | an `exterior_grid` that is not two integers |
| `exterior_grid_not_adjacent` | warning | connected exterior cells squares apart |
| `exterior_terrain_undersized` | warning | terrain that does not fill its placed cell, leaving gaps |
| `portal_off_navmesh` | yes | a connection's spawn point that lands on no walkable polygon |
| `dangling_reference` | yes | Octopus's own dangling-ref check |
| `structure_without_state` | warning | baked geometry with no `StructureState`, so damage to it could never be remembered |
| `location_without_cell` | warning | a Location the manifest bakes no cell for |
| `cell_without_location` | warning | a manifest cell that is not a Location |
| `over_budget` | warning | declared costs exceeding the cell's declared budget |

The spawn-path rule is the one §4 called out by name, and it landed as
`lobster/build/lint.py::check_spawn_paths` — asserted by
`tests/test_build.py::test_an_unenterable_location_is_an_error`. Both halves are
checked: a Location is enterable if it declares `default_spawn_transform` **or**
if any other Location's connection *pointing at it* carries a `spawn_transform`.
Neither one is enough on its own to be silently assumed.

```bash
python -m lobster.cli lint world.manifest.json          # human-readable
python -m lobster.cli lint world.manifest.json --json   # machine-readable
```

Exit status is `1` when any error-level finding is present, `0` otherwise.
