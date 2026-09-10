# Lobster decision log

Every entry here exists because the Scope left something ambiguous, or because
an integration detail in Octopus forced a choice. The rule from the brief:

> When an ambiguity arises, choose in this order:
> 1. Preservation of the cheap contract and zero-policy rule (L7/L8)
> 2. Save / mod compatibility (StructureState, quarantine, no private formats)
> 3. Deterministic, attributable failure
> 4. Performance (after correctness)
> 5. Implementation simplicity (last)

Nothing is decided silently. Each entry quotes the Scope passage it answers.

---

## D0 — Octopus's real simulation-tier names (blocking, §7/§16)

**Scope (§7):** "This still needs a direct check against
`nested-object-engine-sds.md` before any code lands — if Octopus's real
simulation tiers share a name with the tier below, adopt that name for the
shared concept."

**Check performed.** Octopus's tiers are declared in
`../Octopus/lce/npc.py:20-22` and specified in
`../Octopus/nested-object-engine-sds.md` §9.2:

```
TIER_ACTIVE  = "ACTIVE"
TIER_NEARBY  = "NEARBY"
TIER_DORMANT = "DORMANT"
```

**Findings:**

1. `ACTIVE` and `DORMANT` are shared names for shared concepts. Lobster keeps
   both spellings, and `lobster.tiers` asserts string equality against
   `lce.npc.TIER_ACTIVE` / `TIER_DORMANT` at import time so a rename in
   Octopus fails loudly here instead of drifting.
2. The v0.2 middle tier `NEARBY` **would have collided**. Octopus's `NEARBY`
   means "adjacent per the Location `connections` graph" (SDS 9.2 /
   `npc.assign_tier`). Lobster's middle tier means "within projectile range,
   *regardless of connection-graph adjacency*" (§7). Same word, genuinely
   different question — v0.3's rename to `PROJECTILE` is vindicated and is
   now enforced: `lobster.tiers.PROJECTILE` is asserted **not** to be any of
   Octopus's tier names.
3. Octopus's `resolve_npc_state(..., tier=None)` treats an unspecified tier as
   *not*-ACTIVE, deliberately (DESIGN_NOTES D24) — attack/flee/hide packages
   are skipped. That is exactly the guarantee §7 needs for "PROJECTILE is a
   hit-test-candidacy tier, not a simulation-promotion tier": Lobster passes
   `TIER_ACTIVE` **only** for entities at Lobster's own ACTIVE tier, and
   `None` for PROJECTILE and DORMANT. Enforced in `octopus_bridge` and
   asserted in `tests/test_tiers.py`.

**Decision:** ACTIVE/DORMANT adopted from Octopus; PROJECTILE stays
Lobster-owned and is never passed to any Octopus API.

---

## D1 — `.lobster_cell` vs. "no private save format"

**Scope (§13):** "Geometry is data — no private save format anywhere."
**Scope (§4.5):** "Runtime bundle: one `.lobster_cell` per Location id."

These only conflict if `.lobster_cell` holds state. It does not.

**Decision:** `.lobster_cell` is a *derived build artifact*, a pure function of
(Octopus content packages + `.vox` files + world manifest). It is deletable and
rebuildable at any time, contains nothing a player's session can change, and is
never written at runtime. Everything mutable — break-state, spawn transforms,
zone shapes, sound sources, structure→Location placement — is an Octopus record
or field, resolved through the ordinary layer stack. `lobster.cell` has no
write path, by construction: `CellBundle` is a frozen dataclass and the loader
opens files read-only.

Consequence, accepted deliberately: moving a structure or reshaping terrain
requires a rebuild, which §4.5 already states ("Hot reload: not supported in
v1. Rebuild + relaunch, stated plainly").

---

## D2 — Where Lobster's Octopus-side fields are declared

Octopus's day-one catalog (`../Octopus/lce/catalog.py`) has no
`Character.limb_state`, no `Location.default_spawn_transform`, no
`Zone` shape field, no sound-source field and no `StructureState` type. All of
these are content-visible data the Scope requires.

**Decision:** they are declared as an ordinary SDS 7.2 package schema
extension (`packages/lobster_geometry.json`), which is the sanctioned public
path — not engine edits, not a private format. Lobster never writes to the
Octopus tree.

`Character.limb_state` is deliberately split into a **separate** package
(`packages/lobster_limb_state.json`) because `SchemaRegistry.apply_extension`
raises when two different packages declare the same field
(`../Octopus/lce/schema.py:196`). Shrimp or Octopus proper may well want to own
that declaration; splitting it lets a project drop Lobster's copy without
losing the rest. `lobster.octopus_bridge.limb_state` treats an absent field or
absent record as **all limbs intact** — the geometric default, and the
pre-existing behaviour of a body with no limb data. A severed limb must be
stated, never inferred.

---

## D3 — `destroyed_chunks` is not `save_layer_only`

**Scope (§6):** "two mods damaging different walls of the same keep should
both apply."

A `save_layer_only` field rejects writes from content packages
(`CONTENT_FORMAT.md` §7). That would make the quoted sentence impossible.

**Decision:** `StructureState.destroyed_chunks` is `UNION_TOMBSTONED` and
*not* save-layer-only, so a mod may ship a pre-ruined keep and a save may add
to it, with the union resolving exactly as §6 describes. `location_id` is
`REPLACE`.

---

## D4 — Severing a connection uses `DELETE_ENTRY`, not `PATCH`

**Scope (§10.4):** "that's a `patch_record` against the Location's
`connections` field, going through Octopus's ordinary record-update path".

`Location.connections` is `UNION_TOMBSTONED`
(`../Octopus/lce/catalog.py:207`). A `PATCH` on a union field is rejected by
`_validate_against_schema`; the operation that removes one entry from a
tombstoned union is `DELETE_ENTRY`.

**Decision:** read "`patch_record`" as the Scope's generic phrase for "an
ordinary Octopus record update", and emit the op the declared merge policy
actually calls for: `DELETE_ENTRY(location_id, "connections", <entry>)`. Entry
identity is canonical whole-value equality (Octopus DESIGN_NOTES D2), so the
entry is passed back **verbatim as resolved**, never reconstructed field by
field.

---

## D5 — `on_hit_location.region` is nullable for PROJECTILE-tier hits

**Scope (§7):** PROJECTILE-tier hitbox fidelity is a "single whole-body
capsule". **Scope (§5):** the hit-region vocabulary is "a Lobster-owned enum (6
humanoid regions)".

A whole-body capsule test does not know which of the 6 regions was struck.
Reporting one anyway would fabricate precision Lobster never measured — a
policy decision dressed as data, which L4 forbids ("Lobster reports; it does
not decide").

**Decision:** `region` is `Optional[HitRegion]`. `None` means "whole-body
capsule, no region resolved", and only ever appears on a PROJECTILE-tier hit.
The 6-region enum is unchanged and un-extended. What a region-less hit *means*
is Shrimp's call.

---

## D6 — Stdlib only

Octopus ships with no third-party dependencies. Lobster matches it: voxel grids
are `bytearray`, meshes are lists of tuples, tests are `unittest`. L7 ("any
bloat in a finished build must be traceable to content, never to the shell")
reads as an argument against a dependency tree for the shell.

---

## D7 — Sound sources, zone shapes and spawn transforms live in records, not
in the bundle

**Scope (§4.5):** the bundle carries "prop and **sound metadata**".
**Scope (§4.5), two sentences later:** "Any mod-supplied addition to any of this
— including a sound source list — goes through the same
operation-log/merge/quarantine path as any other package content; the build step
must not special-case it into a bypass channel."
**Scope (§13), invariant 1:** "no private save format anywhere, including a
mod's sound-source list."

A bundle copy that the runtime *reads* is a bypass channel even if nobody ever
writes to it: a mod that layers a new sound source onto a Location would be
ignored, because the runtime was reading the baked copy instead of the resolved
record.

**Decision:** `Location.sound_sources` (UNION_TOMBSTONED), `Zone.shape` and
`Location.default_spawn_transform` / per-connection `spawn_transform` are
Octopus fields, resolved at load time. `ResidentCell.sound_sources()` reads the
record. The bundle carries none of them — only `provenance`, which names the
records it was baked from so a failure can name them too. "Cell metadata" (§3)
is satisfied by the fact that a cell *is* a Location.

---

## D8 — Only exteriors preload a ring

**Scope (§1):** "One cell (plus immediate exterior neighbors) resident at a
time."

Octopus's only notion of adjacency is `Location.connections`, so that is what
the ring is computed from — Lobster inventing a cell coordinate grid would be a
second source of truth for which cells are next to each other. But an interior
hub with twelve doors has twelve "neighbours", and preloading all of them is
both wrong and the fastest way to blow the transition budget.

**Decision:** a Location tagged `exterior` is an exterior cell and preloads its
exterior neighbours; everything else is an interior and preloads nothing. The
absent tag is the conservative default (fewer cells resident, never more), and
the word "exterior" in §1 is doing exactly this work.

---

## D9 — Reading `connections` is not a new entry on the permitted query list

**Scope (§13)** lists seven permitted live queries. Residency (§1), the
adjacency half of the navmesh patch (§10.3) and connection-severing (§10.4) all
need the connection graph.

**Decision:** `FrameView.connections` / `connection_path` delegate to
`lce.graph`, which is a pure traversal of the `Location.connections` *field* —
the same record read `FrameView.record` performs, with the entry shape unpacked.
It is routed through the frame view so the closed-view rule applies, and it is
not added to `PERMITTED_QUERIES`, because the Scope's list is of derived,
gate-evaluated queries, not of record reads. Re-deriving adjacency inside
Lobster was the alternative, and it would be a second source of truth.

---

## D10 — The world tick reads bundle *headers*

**Scope (§13):** the structure queries "are what let the world tick apply
mass-casualty structure damage without Lobster being loaded or involved at all."

Almost. `destroyed_chunks` holds chunk indices, and a chunk index is geometry:
something has to know where a structure stands and how big its chunks are, and
Octopus has no geometry at all.

**Decision:** `lobster.worldtick` resolves an unwitnessed blast from the bundle
**header** — structure id, origin, `grid_size`, `chunk_size` — which is a few
hundred bytes, needs no voxel payload, no mesh, no navmesh and no cell load.
"Without Lobster being loaded" is honoured in the sense that matters: no cell
becomes resident, nothing is meshed, and the operation written is byte-identical
to the one a witnessed hit would have written.

---

## D11 — Greedy meshing is implemented twice, on purpose

**Scope (§3):** "Greedy meshing — Build-time for terrain; touched-micro-chunk-only
at runtime for structures."
**Scope (§15.1):** "Don't let terrain and structures share a code path."
**L2:** "Terrain and structures are never the same system."

Sharing one greedy mesher between them is the obvious engineering move and it is
exactly what §15.1 forbids. Ordinarily duplicating an algorithm would be a
defect, so this is written down rather than left to be discovered.

**Decision:** two meshers, sharing no helper, no quad type, no import.
`lobster/structure_mesher.py` is runtime, per-micro-chunk, and reads solidity
through the break-state mask. `lobster/build/terrain_mesher.py` is build-time,
whole-cell, and column-oriented.

The duplication turned out to be small, and the two diverged immediately anyway
— which is the argument for the rule rather than an accident of it. Terrain is a
*surface* that never changes, so its mesher walks columns and is O(columns); a
structure is a *volume* that does change, so its mesher walks voxels inside one
8³ chunk and has to get the chunk boundary right so that destroying a chunk
exposes its neighbours. A shared implementation would have been a shared
compromise.

`tests/test_build.py::test_the_two_meshers_share_nothing` asserts the
separation, alongside the §14 test 1 separation checks.

---

## D12 — Terrain has its own voxel size

**Scope (§14)** declares 0.25 m/voxel for *structures* and says nothing about
terrain.

A 128 m cell at 0.25 m/voxel is 512³ voxels. That is the wrong resolution for a
static landscape, and coupling the two resolutions would be exactly the kind of
shared assumption L2 exists to prevent.

**Decision:** `TERRAIN_VOXEL_SIZE_M = 1.0`, declared separately in
`lobster/constants.py`, adjustable per cell through the manifest's `navmesh`
block. Structures keep `VOXEL_SIZE_M = 0.25` unchanged. Neither constant is
derived from the other.

---

## D13 — `region=None` never appears on an ACTIVE-tier hit

A consequence of D5 worth stating separately, because it is what Shrimp binds
against: `on_hit_location.region` is `None` **only** for a PROJECTILE-tier hit
resolved against the whole-body capsule. An ACTIVE-tier hit always names one of
the six regions, because a per-bone test measured one. A consumer can therefore
read `region is None` as "no region was measured", not as "no region was hit".

---

## D14 — Which cell's coordinate space a Zone's shape is expressed in

**Scope (§4):** "`Zone` → trigger volume, shape via `shape_ref`: `box`,
`cylinder`, or `polygon`."

The Scope names the primitives and freezes them, but says nothing about the
frame those coordinates are measured in — and that is not a detail. Octopus has
no geometry at all, and a `Zone` may reference several `Location`s
(`Zone.location_refs`), so `center: [64, 2, 64]` is meaningless until something
says *whose origin*. Cells are 128 m and each has its own origin, so guessing
wrong puts a trigger volume in the wrong village.

**Decision:** Lobster's schema extension declares a second field,
`Zone.shape_location_ref` (`REPLACE`, default `null`), naming the cell whose
space `shape` is in. `lobster.zones.volumes_for_cell` resolves it:

- **set** → the volume exists in that cell only;
- **null** → the volume is offered to every cell in `location_refs`, each
  interpreting the coordinates in its own local space.

The `null` fallback is for the common single-Location Zone, where there is only
one possible answer and asking an author to state it would be ceremony. It is
deliberately *not* silent about the multi-Location case being different:
`CONTRACT.md` §7 tells authors to set the field whenever a Zone spans more than
one Location, because the fallback then produces the same box in every cell,
which is almost never the intent.

The alternative — inferring the frame from `location_refs[0]` — was rejected
under rule 3 of the ambiguity order (deterministic, attributable failure): it
would silently pick a cell based on authoring order, and moving a Location up
the list would move a trigger volume across the world with no error anywhere.

Documented in `CONTRACT.md` §7; the fallback and the explicit case are both
covered by `tests/test_presentation.py::TestZoneVolumes`.

---

## D15 — `spawn_transform` is a key on the `connections` entry

**Scope (§4):** "`connections` → doors/portals; **the connection owns the spawn
point.** Matches Oblivion's pattern; lets two doors into the same room arrive at
different spots."

Octopus's `connections` entries are `{target_location_id, label, condition,
travel_cost?}` — there is no spawn point, because Octopus has no geometry. The
spawn point has to live *on the entry* rather than on the Location, or two doors
into the same room cannot arrive at different spots.

**Decision:** an optional `spawn_transform` key on the connection entry itself,
`{"position": [x, y, z], "rotation": [x, y, z, w]}`. The transform describes
where an agent arrives **in the target cell**, so the door into `cell-village`
is the entry on `cell-field`'s connection *pointing at* `cell-village` —
`CellManager.spawn_transform(view, cell_id, from_location_id)` reads it that way,
and the navmesh bake attaches portals the same way.

**The gotcha authors need, and the reason this has its own entry:**
`Location.connections` is `UNION_TOMBSTONED`, and Octopus resolves entry identity
by canonical whole-value equality (its DESIGN_NOTES D2 — noted in
`lce/catalog.py` beside the `travel_cost` field for exactly this reason). Adding
`spawn_transform` to a connection entry therefore **changes that entry's
identity**. Consequences:

- author `spawn_transform` from the start, in the package that creates the
  connection; adding it later is a different entry, not an edit;
- a mod removing a connection must `DELETE_ENTRY` the entry *verbatim as
  resolved*, spawn transform included — which is why
  `lobster.connection_graph.connection_entry` returns the resolved value
  untouched and never rebuilds one field by field (D4).

The build-step lint enforces the half the Scope named: a Location with neither
`default_spawn_transform` nor an incoming connection carrying a
`spawn_transform` is an `unenterable_location` **error**, so an unreachable
Location fails the build rather than surfacing the first time somebody fast
travels there.

---

## D16 — Projectile hits resolve to a limb (supersedes D5)

**Authorised by the project owner as an executive call.** This is a deliberate
departure from the Scope, recorded as one rather than papered over.

**Scope (§7)** declares PROJECTILE-tier hitbox fidelity as "Single whole-body
capsule", and **D5** followed that: a whole-body capsule cannot know which of
the six regions was struck, so `region` was `None`.

**The objection, and it is right:** that generalises away the whole point of
having limbs at range. A sniper's shot and a stray arrow report identically, and
no downstream system can tell a headshot from a hit in the leg. The fidelity
choice was made for cost, so the question is what it actually costs.

**Measured, on a 200-strong PROJECTILE-tier army:**

| | |
|---|---|
| whole-body capsule test | 3.5 µs |
| 6-bone refinement | 21.4 µs (6.1×) |
| bone refinements per landed hit | **1.00** |
| 1,000 arrows: capsule tests | ~1,000 |
| 1,000 arrows: broad-phase candidates | ~76,800 |

The refinement is gated behind the body-capsule test, so a **miss costs exactly
what it cost before** and only a landed hit pays for detail. On a
thousand-arrow volley that is about 1,000 capsule tests — roughly 3 ms, and not
remotely the bottleneck.

**Decision:**

- PROJECTILE-tier fidelity becomes *whole-body capsule to decide **if**, six
  regions to decide **where***.
- `HitResult.region_precise` distinguishes a measured intersection (`True`)
  from the nearest bone when the ray clipped the body capsule without touching
  a bone (`False`). Both are measurements — every capsule is tested and the
  minimum taken — but a consumer can tell them apart, so L4 survives: Lobster
  still reports only what it measured.
- `HitResult.pose_version` carries how fresh the pose that resolved the region
  was, the counterpart to `snapshot_seq` for position. A distant target's pose
  is whatever was last set, and the result says so instead of implying it was
  live.
- The **limb-state read now applies at every tier**. §5 puts no tier condition
  on it, and "a severed limb offers no hitbox" is as true of a target two cells
  away as of the man in front of you. This is strictly more correct than before.
- `resolve_blast` resolves regions the same way, `region_precise=False` — there
  is no ray, so the region is the bone nearest the epicentre. Leaving blasts
  region-less while projectiles resolved would have been incoherent.
- `region` is now `None` only when the target has no rig at all.

**What is explicitly NOT changed:** the load-bearing half of §7 — *"PROJECTILE
is a hit-test-candidacy tier, not a simulation-promotion tier"*.
`octopus_tier_for(PROJECTILE)` is still `None`, the refinement uses only pose
data Lobster already holds plus a pure query, and nothing on this path wakes an
NPC. `tests/test_projectile_snapshot.py::TestProjectileDoesNotPromoteSimulation`
still passes untouched.

**The `on_hit_location` wire event is unchanged.** §13 fixes its four fields;
`region_precise` and `pose_version` live on Lobster's own `HitResult` alongside
the existing snapshot provenance, not on the Event.

### Two defects this surfaced

**1. The broad phase bounded the segment with a sphere.** `query_segment` took a
100 m arrow, wrapped it in a 50 m sphere, and returned *every entity in the
cell* as a candidate: one arrow examined all 400 bodies in 1.69 ms and hit
nothing. That is O(arrows × population), the exact scaling §7 exists to avoid.
It now walks the grid along the segment: 400 candidates → 82, 1.69 ms → 0.39 ms,
and an arrow's cost no longer grows with the crowd it flies past. This was
wrong before D16 and independent of it.

**2. A headshot was culled before any capsule was tested.** The broad radius was
a hardcoded 1.5 m measured to an entity's *position*, which is at its feet — so
a hit 1.69 m up a 1.8 m rig never reached the narrow phase. Now
`PROJECTILE_BROAD_RADIUS_M = 2.5`, declared, with the reasoning that it must
span the tallest supported rig or hits are silently lost. Also a pre-existing
bug.

### The volley, budgeted

The worst case is real, and it is not the bone test — it is the broad phase, at
~77 candidates per arrow. Two ceilings are now declared and enforced per frame,
naming the cell and the alternative:

- `MAX_BROAD_CANDIDATES_PER_FRAME = 32768` — above a serious skirmish, below a
  thousand-arrow volley;
- `MAX_CAPSULE_TESTS_PER_FRAME = 4096` — generous, since refinement is per
  landed hit.

A thousand-arrow volley trips the first and is told what to do instead: that is
a mass-casualty event, and §6.5/§7 already have a path for it — a zone-occupancy
query resolved once, not a thousand ray tests. The shell fails loudly rather
than quietly spending half a second on the wrong shape of work (L6).

---

## D17 — Batch structure damage (Shrimp wishlist #1)

**The ask:** a bulk-apply path for siege resolution -
`writer.destroy_many([(structure_id, chunk_indices), ...])` - explicitly framed
as *"purely about call-count/efficiency at siege-resolution scale, not
capability"*.

**The premise did not hold, and the real gap was elsewhere.** Measured on 200
structures:

| | |
|---|---|
| 200 × `destroy()`, no reads between | **1.5 ms** (7 µs each) |
| one lazy resolution afterwards | 3.3 ms |
| 200 × `destroy_many()` | 1.3 ms |
| 200 × `destroy()` **with a resolution read after each** | 19 ms (4×) |

Octopus's resolution is lazy and incremental (its D29/D47), so N writes queue and
one re-resolution settles them. Looping `destroy` was already cheap. The only 4×
on the table is reading the resolution between writes, which is a caller-side
mistake a batch API happens to make unavailable.

What looping `destroy` genuinely lacked, confirmed by reproduction:

**1. Atomicity.** A siege list containing one structure that does not resolve
failed on that entry with earlier ops already committed and their Events already
fired - 2 of 4 written, town half-sacked, no rollback. For an offscreen resolver
that is a corrupt outcome nobody can recover from.

**2. Freedom from redundant writes.** Ten re-runs of the same siege appended 50
operations, 45 of them no-ops that `UNION_TOMBSTONED` folded away at resolve
time. The resolved world stayed correct while the save file grew forever. An
offscreen resolver runs for game-years, so that is unbounded growth traceable to
the shell's API shape rather than to content - which L7 forbids.

**Decision:** ship `destroy_many`, but on those grounds rather than the stated
one, and say so plainly in `CONTRACT.md` so Shrimp does not adopt it expecting a
speedup that is not there.

- **Atomic**: everything is validated before anything is written; a bad entry
  rejects the whole batch having written nothing and fired nothing.
- **Redundancy-suppressing**: one resolution read for the batch, chunks already
  destroyed are skipped and reported in `BatchDamage.skipped`.
  `skip_redundant=False` restores unconditional writes.
- **Coalescing**: two entries for one structure become one operation and one
  Event, because that is one structure taking damage.
- `destroy()` is **unchanged** - it stays the cheapest single call and does not
  force a resolution read. The split is deliberate: the batch path can afford
  one read amortised over many structures, the single path cannot.

**Not changed:** the Event shape. §13 fixes `on_structure_damaged(structure_id,
chunk_indices[])`, so a batch fires one per structure and no batch Event was
invented. Validation also stops at what the writer can see - it does not check
indices against `grid_size`, because that needs the authored bundle and §6
already handles a stale index at load time by quarantining it with a reason.

`lobster.worldtick._resolve` now goes through this path, since an unwitnessed
blast reaching twenty buildings is the same shape of work and deserves the same
all-or-nothing guarantee.

---

## D18 — A tier is a query answer, not an entity class

**Surfaced by Shrimp's scope document**, which proposed:

> Mobs are lighter-weight than named NPCs — likely no full Octopus record,
> PROJECTILE-tier by default until the player closes distance.

That is a misreading, and three separate things in it point the wrong way:

1. **"by default"** treats the tier as a persistent property of a kind of
   creature. It is per-entity, per-cell-residency runtime state, passed to
   `cell.place()` and changed by `set_tier`. Nothing in Lobster stores a tier on
   a record or an archetype.
2. **"until the player closes distance"** reads it as an LOD ramp keyed on
   player proximity. Scope §7 scopes PROJECTILE by *the attack being resolved* -
   "any cell within current spell/projectile range" - not by distance to
   anybody. A fireball two connections away creates candidacy for that one test.
3. **"lighter-weight"** picked the wrong tier for the right instinct. DORMANT is
   the cheap one: no rig, no per-frame cost, no hit-test. PROJECTILE requires a
   rig and pays a body-capsule test per shot.

**Measured**, 120 mobs in one cell, a 20-arrow skirmish:

| | |
|---|---|
| all PROJECTILE (their reading) | 9.11 ms, 238 body tests, 120 rigs, 61 KB |
| all DORMANT (intended) | 6.33 ms, 0 body tests, 0 rigs, 0 bytes |

Worse, the two halves of their proposal combine into a silent failure:
a rig-less "lightweight mob" at PROJECTILE tier **could not be hit at all** -
`resolve_projectile` skipped any candidate with no registered `Skeleton`, so
arrows passed straight through with no error anywhere.

**Two decisions:**

**1. That silence was a defect in Lobster, and is fixed.** ACTIVE and PROJECTILE
both declare a hitbox (`tiers.has_hitbox`), so an entity at either with no rig is
an integration bug. It now raises `HitTestError` naming the entity and both
remedies - register a rig, or use DORMANT. §13 invariant 2 requires failure to be
visible and attributable, and silently unhittable is the opposite.

**2. The ambiguity was partly ours.** `CONTRACT.md` §3's tier section explained
what tiers *do* without ever saying what a tier *is*, which is what left room to
read it as an entity class. It now leads with "a tier is not an entity class",
gives the per-tier cost table, and states that DORMANT is the cheap default for
a mob nobody is fighting.

No behaviour of the tiers themselves changed. Tier semantics are as Scope §7
declares them; what changed is that misusing them now fails loudly and the
documentation says what they are.

---

## D19 — Gating volumes are derived from the rig (Shrimp #4 and #5)

Two findings from Shrimp's first real content build, reproduced here before
anything was changed. **One root cause:** a volume that gates a hit test was
built without reference to how much space the thing being tested occupies.

**#4 — `whole_body_capsule` assumed a vertical cylinder.** Its radius came from
`max(bone.radius)`, which bounds a rig only if every bone lies within the widest
bone's radius of the vertical axis. A humanoid assumption, stated nowhere.

| rig | body-capsule radius | actual horizontal reach |
|---|---|---|
| humanoid | 0.198 m | 0.369 m — **under by 0.17 m** |
| fen-spider (2.4 m, 8 legs) | 0.280 m | 1.250 m — **under by 0.97 m** |

At PROJECTILE tier the capsule gates the ray, so three quarters of the spider
was unshootable at range and hittable in melee — silently. The humanoid was
wrong too and got away with it because the overhang was thin limb.

**#5 — the swing broad phase measured to the feet.** `resolve_swing` culled on
`distance(swing midpoint, entity position)` against `half-length + radius`, and
an entity's position is at its feet. A 1 m blade with 0.15 m radius swung at
chest height is 1.2 m from the point it is measured against:

| swing height | result | capsule tests |
|---|---|---|
| 0.2 m (ankle) | `left_leg` | 6 |
| 0.9 / 1.2 / 1.7 m | **nothing** | **0** |

Zero capsule tests — the entity never became a candidate. Melee only connected
at ankle height. Lobster already knew about this on the projectile path; the
comment on `PROJECTILE_BROAD_RADIUS_M` describes the exact failure, and the
swing path simply had no equivalent.

**Decision: measure the rig, once, and derive every gate from it.**

- `RegionSet.extent` (`RestExtent`) is computed at construction — `min_y`,
  `max_y`, `horizontal_reach`, `bound_radius` — so this is O(1) at query time.
- `whole_body_capsule` uses the rig's full Y span as its axis and
  `horizontal_reach` as its radius. Every point of every bone is inside by
  construction: a point's height is within the span, so the nearest point on the
  axis is directly beside it at exactly its horizontal distance. Asserted for
  three rigs by sampling every bone's capsule surface.
- `HitTester.broad_margin()` is `max(PROJECTILE_BROAD_RADIUS_M, widest
  registered rig's bound_radius)`, and **both** the swing and projectile broad
  phases use it. Shrimp's note that the constant "is a *height* argument for a
  radius" was right: a 6 m wyrm derives 6.5 m and is now shootable in the head,
  where the fixed 2.5 m culled it one stage earlier.
- Bounds carry a 1 µm epsilon. An exactly-touching bound is decided by
  floating-point rounding, and for a gate the safe direction is outward:
  over-inclusion costs one refinement that finds nothing, under-inclusion
  silently loses a hit.

The spider now resolves the same leg at PROJECTILE that it does at ACTIVE, which
is what CONTRACT §1 promised and did not deliver. Pinned by
`tests/test_gating_volumes.py`; Shrimp's two `expectedFailure` tests should now
be unexpected successes.

---

## D20 — The per-cell byte default is derived from the peak (Shrimp #3)

**Scope (§14)** asks for a per-cell budget and a transition-peak ceiling, and
Lobster picked both by hand. Shrimp pointed out they contradict:

> `DEFAULT_MAX_CELL_BYTES` is 48 MiB, `MAX_RESIDENT_CELLS` is 9, and
> `MAX_TRANSITION_PEAK_BYTES` is 192 MiB. Each is reasonable alone. Together:
> 9 x 48 = **432 MiB against a 192 MiB peak** [...] a cell that declares nothing
> gets a default it can never be allowed to actually use.

Correct, and the obligation to declare lower was written down nowhere. A default
that cannot compose is a lie.

**Decision:**

1. `DEFAULT_MAX_CELL_BYTES = MAX_TRANSITION_PEAK_BYTES // MAX_RESIDENT_CELLS`
   — 21.3 MiB. **Superseded by D51**, which gave the shared model library a
   share of the same peak: the divisor is now `RESIDENT_MEMORY_SHARES`
   (`MAX_RESIDENT_CELLS + 1`) and the default is 19.2 MiB. The *argument* below
   is unchanged and is what forced the recalculation. A full residency of all-default cells now fits by construction
   rather than by anyone remembering. (Shrimp derived 20 MiB independently from
   the same peak, which is the convergence you want.) This is a real change in
   behaviour: content that fit in 48 MiB and not 21 MiB now fails at load rather
   than at a transition, which is the earlier and more attributable of the two.
2. `budgets.transition_peak_findings` implements the check as asked — for each
   cell, the worst transition out of it, summing declared `max_bytes` over the
   union of both residency sets against the peak, reported as
   `over_transition_peak` naming the cells in that union. `lobster budgets`
   runs it and exits non-zero. The *actual* baked total is reported beside the
   declared one, because a cell may declare 21 MiB and use 3, and only the
   second number predicts a real failure.
3. `residency_ring` was extracted from `CellManager.desired_residency` so the
   loader and the checker share one definition of the rule. A prediction built
   on a second copy of the residency logic would eventually be about a different
   world than the one that loads.
4. CONTRACT §6 gains the line Shrimp asked for: *a cell's declared `max_bytes`
   must leave room for its ring, not just for itself* — with the eight-way walk
   union spelled out.

---

## D21 — Exterior cells declare where they are

**Authorised by the project owner.** Building the visibility pass surfaced a gap
neither the Scope nor Octopus had an answer for: **cells had no spatial
relationship to each other.** A `Location` originates at (0, 0, 0) and a
connection carries where an arrival *lands* (§4, "the connection owns the spawn
point") — not where the neighbouring cell *sits*. Octopus has no geometry at all.

Consequence, only visible once something tried to draw: a renderer could draw
**one cell**, ever. For a Morrowind-style interior that is correct. For an
exterior world it means the ground stops at the cell boundary and the next field
over cannot be drawn, because there is no offset to draw it at.

**Decision: exterior cells author `Location.exterior_grid: [x, z]`**, integer
grid coordinates, placed at `[x * EXTERIOR_CELL_SIZE_M, 0, z * EXTERIOR_CELL_SIZE_M]`.

- **Bethesda's scheme, which is the already-solved answer** (L1). Integer
  coordinates cannot drift, adjacency is a comparison rather than a tolerance,
  and the cell size is already a locked constant (§14).
- **It is a record field**, declared in `packages/lobster_geometry.json`, so a
  mod can move a cell through ordinary layered content — geometry is data
  (§13 invariant 1, D1/D7).
- **Interiors are placed nowhere** and resolve to the identity. An interior *is*
  its own coordinate space; that is exactly what makes the cell model cheap, and
  placing one would invent a fact.
- **No Y offset.** Height within a cell is the heightfield's job and differences
  between neighbours are baked into their terrain; a per-cell elevation would be
  a second way to say the same thing.

**Residency still comes from `connections`, not from the grid.** D8's argument
stands: `connections` is Octopus's own notion and drives navigation, fast
travel, the lint and the severing path, and a second adjacency notion could
disagree with it. The grid places cells; it does not decide which are resident.
What the two together *do* license is a consistency check, and the build step
now runs four:

| finding | severity | catches |
|---|---|---|
| `exterior_without_grid` | error | a connected exterior with nowhere to be drawn |
| `exterior_grid_collision` | error | two cells claiming one square |
| `exterior_grid_malformed` | error | not two integers |
| `exterior_grid_not_adjacent` | warning | connected cells squares apart — legitimate for a ferry, a typo otherwise |
| `exterior_terrain_undersized` | warning | terrain that does not fill its cell, so the tiled world has holes |

`lobster.visibility` still invents nothing: it takes placements it is handed,
and `CellManager.placements` is what reads them off the records.

**Not addressed here:** cross-cell hit-testing. Hit-tests remain cell-local, as
they always were. Placement now makes a world-space projectile query *possible*;
it is not built, and §7's PROJECTILE tier spanning cells is still unimplemented.

---

## D22 — Lobster uses the GPU when it is available (amends D6)

**Authorised by the project owner**, after the observation that Lobster shipped
every input to a renderer and no renderer, and the follow-up question of whether
that was being solved with the best-known answer or merely finished.

**It was not.** The honest account, because L1 deserves one:

> Every hard problem gets the industry's already-solved answer, chosen on day
> one.

A hand-rolled scanline rasteriser in Python is not the industry's answer to
"draw 3D geometry"; the GPU through a graphics API is. What happened is that
**D6 (stdlib-only) was an implementation preference chosen for the shell, and it
was allowed to pick the answer to a hard problem** rather than being revisited as
the scoped decision it had become. That is L1 exactly inverted, and the tell was
immediate: ~350 lines of rasteriser produced two bugs — near-plane clipping and
winding convention — that every graphics API has solved and documented for
decades, found by squinting at PNGs.

**Decision:**

1. **A GPU backend is used when it is available. ModernGL is the chosen
   answer** — thin, modern OpenGL 3.3+, actively maintained, and it supports
   standalone/headless contexts, so the same backend can render offscreen for CI.
   Naming it now is L1 satisfied: the choice is made, whatever the schedule.
2. **Core Lobster keeps no hard graphics dependency.** `select_backend()` prefers
   the GPU and falls back to software, saying which and why. L7 holds — the shell
   drags in no graphics stack, and every test runs on a machine with neither GPU
   nor display. A test asserts no `moderngl`/`pyglet`/`OpenGL` import at module
   scope in the render package.
3. **Asking for a backend that cannot run raises**, rather than silently falling
   back. A caller who asked for the GPU and quietly got a Python rasteriser would
   draw the right picture and blame the wrong thing for the frame rate.
4. **The software rasteriser is demoted in writing** to reference and fallback.
   It is CI's pixel oracle and the headless path. It is not, and will not become,
   the renderer a player runs.

**The GPU backend is not written.** This machine has no GPU, no display and no
graphics library installed, so writing it would mean shipping code that has never
executed a line — which is the failure mode this whole entry is about. The seam
in `lobster/render/backend.py` is what it must implement, the software backend
proves the seam is sufficient, and `probe()` reports it as unimplemented by name
rather than pretending.

**What the CPU rasteriser earned, and why it stays:** it found a real bug in
Lobster's own data that no geometry test had — the terrain mesher wound its top
surface with the normal pointing *down*, which would break lighting and backface
culling in a GPU renderer too. Fixed, with tests. Building the consumer is what
found it.

---

## D23 — A shot crosses the cell boundary (Shrimp #8)

**A formal ask, and a correct one.** The contract promised something that was
not true:

- CONTRACT §3 defines PROJECTILE as "within the range of a projectile currently
  being resolved, **in whatever cell**";
- Scope §7: "Any cell within current spell/projectile range, **regardless of
  connection-graph adjacency**";
- CONTRACT §1: "a sniper's shot two cells away resolves to a limb exactly as a
  sword blow does."

`HitTester` is built around one `ResidentCell.index`, so a shot stopped at the
cell boundary. I had flagged this as unbuilt when placement landed; D21 turned it
from theoretical into visible, because a player at the gate can now *see* the
next valley. Reproduced before changing anything: a bandit **16.1 m away, inside
a 45 m shot, on screen, and unhittable**.

Shrimp declined to work around it, which was right — clamping shot range to the
cell edge would have hidden a contract promise behind a content number.

**Decision: `WorldHitTester`. The ray moves; the world does not.**

Each cell keeps its own local coordinates, its own spatial index and its own
snapshot provenance. The ray is transformed into each cell's space by the inverse
of that cell's placement, and the ordinary per-cell path runs **unchanged**.

The alternative — merge every resident cell into one world-space index — was
rejected: it means rebuilding an index every frame and discarding the
entry/exit snapshot rule that Scope §7 spends a paragraph justifying, to solve a
problem that a rigid transform of one ray solves exactly.

Consequences worth naming:

- **`Transform.inverse_rotate`** exists now because a direction is not a point.
  Transforming a heading with `inverse_apply` would subtract the placement's
  position from it and quietly bend every cross-cell ray.
- **`HitResult` gains `cell_id` and `distance_from_source`.** A cross-cell hit
  has to say where the target was, and ordering several hits from one shot needs
  a world-space range. Neither touches the wire Event, whose four fields are
  fixed by Scope §13.
- **Nearest-first, decided globally.** `first_hit_only` picks after every cell has
  answered. The test that pins this puts the *far* target in the
  alphabetically-first cell, so an implementation that returned whichever cell it
  asked first would fail it.

### Two defects this found

**1. Phantom Events.** The first version gave each per-cell sub-tester the bus,
so a shot fired an `on_hit_location` for *every* candidate cell before the
nearest was selected — a bandit taking damage from a bullet that stopped in the
guard standing in front of him. Reporting now happens where selection happens:
the sub-testers get no bus, and `WorldHitTester` emits for the results it
actually returns. Pinned by `TestReportingMatchesTheResult`.

**2. A bounding sphere is the wrong bound for a cell.** The first rejection test
used segment-versus-sphere, and a 128 m square that is barely any height gets a
90 m radius — so a shot fired *away* from a cell still "reached" it and paid a
full sweep. Cells are axis-aligned by construction, so
`geometry.segment_aabb_overlap` (the standard slab test) replaced it: tight,
cheap, and still outward-erring via the broad margin.

### Deliberately not done

- **Cross-cell melee.** ACTIVE is "current cell + immediate melee range"; a
  sword does not reach the next cell. `WorldHitTester` has no `resolve_swing`,
  and a test asserts it stays that way — this is a definition, not an omission.
- **Cross-cell blasts.** Scope §6.5 already resolves mass-casualty through zone
  occupancy, which is Octopus records and cell-agnostic by construction. The
  asymmetry is principled: projectiles had no cross-cell path, blasts always did.
- **Tier changes.** None. Being shot from another cell promotes nothing, and
  `octopus_tier_for(PROJECTILE)` is still `None`.

---

## D24 — Three contract Events are declared but not yet raised by Lobster

**Found by auditing CONTRACT.md for the failure mode D23 exposed**: a document
promising something the code does not do.

CONTRACT §1 was headed *"Events Lobster fires"* and listed seven. Lobster raises
**four**:

| raised by Lobster | raised by the caller |
|---|---|
| `on_enter_cell`, `on_exit_cell` (`CellManager`) | `on_interact` |
| `on_hit_location` (`HitTester`, `WorldHitTester`) | `on_item_placed` |
| `on_structure_damaged` (`CellManager`, `StructureStateWriter`) | `on_item_removed` |

**Why**: all three of the unraised ones would come from Scope §8, which gives
Lobster *"object selection/raycasting, world-space labels, physically
placing/removing an item's representation in the world"* — and **none of that
surface is built**. There is no picking, no world-space label, and no item
representation to place or remove. The events are the visible end of a missing
subsystem, not three missing calls.

**What is not missing**: the payload types, `EventBus.interact()` /
`.item_placed()` / `.item_removed()`, and `OctopusEventSink` routing
`on_interact` to `session.interact()` all exist and work. A caller that does its
own picking can fire them today and everything downstream behaves.

**Decision — correct the claim now, do not build §8 unasked.**

1. CONTRACT §1 gains a **"Raised by"** column and says plainly which three await
   §8, with the note that a caller must do its own picking today. A document that
   overstates the code is the defect this audit was looking for; leaving it
   overstated while adding a subsystem nobody asked for would be two mistakes.
2. `tests/test_contract.py::test_which_events_lobster_itself_raises` pins the
   split by walking `lobster/` for `bus.*` calls, so it cannot drift in either
   direction — a newly-raised event or a quietly-dropped one both fail it.
3. Scope §8 stays open and is now named as such rather than implied by a table.

**The rest of the audit came back clean**, and is recorded here so the next one
can start from a known state: all 18 documented lint codes are genuinely emitted;
all 22 documented API symbols import and exist; §5 invariant 3 ("nothing in
`lobster/` names a location, creature, faction or item") holds against executable
code with docstrings and comments stripped; and the two "pending Octopus" entries
in §3 plus the unimplemented `moderngl` backend in §7 were already disclosed
rather than promised.

---

## D25 — Object selection/raycasting (Scope §8 item 1)

**Asked for directly** ("Let's go with 1"), off the three-item list D24 left
open. Scope §8 assigns Lobster:

> | **Lobster** | Object selection/raycasting, world-space labels, physically
> placing/removing an item's representation in the world. |

This builds the first. `lobster/selection.py`, `tests/test_selection.py`,
CONTRACT §8. It is squarely geometry: a ray, and what is under it.

### Ambiguity 1 — is selection just hit-testing with a different caller?

They look alike and they are not. Merging them was the tempting move and it
would have been a defect in both directions: selection would inherit tier
filtering (you could not point at a DORMANT cart), and hit-testing would
inherit terrain and props (a sword swing that "hits" the ground).

| | `HitTester` | `Selector` |
|---|---|---|
| asks | does this attack connect, how hard, on which limb | what is under this ray |
| considers | rigged bodies, filtered by tier | entities, structures, props, terrain |
| reports | `force`, an `on_hit_location` Event | point, normal, distance |
| budget | `begin_frame()` ceilings | none — one ray, on demand |
| no rig | **raises** `HitTestError` | returns `None` |

That last row is the one that matters. An attack that cannot resolve is an
integration bug and must be loud (the defect `_require_rig` was written to
catch). *Pointing at empty air is not a bug*, so a pick that finds nothing
returns `None` and says nothing. Same maths, opposite failure semantics.

**Decision: two modules, sharing `lobster.geometry` and nothing else.**

### Ambiguity 2 — what does selection get to decide?

Resolution order puts **cheap contract + zero-policy (L7/L8) first**, and this
is exactly where a selection system usually leaks policy: an `is_interactable`
flag, a "prefer NPCs over walls" ordering, a "usable" filter.

> **L4** — Lobster does not decide *what a thing is for*.

**Decision: nearest wins, and nothing else is offered.** It is the only ordering
geometry supports; every other ordering encodes what matters, which is content's
call. `tests/test_selection.py` asserts `Selection` and `Selector` grow no
attribute named `is_interactable`, `usable`, `can_use`, `priority` or
`importance`, so the leak cannot land quietly later.

`interact()` follows the same line: Lobster resolves *what was pointed at* and
raises `on_interact`. The decision that an interaction happened stays outside —
only the consumer knows a button was pressed. Terrain is excluded from
`interact` by default because `on_interact(target_id)` cannot usefully say "the
ground": the id would be the cell.

### Ambiguity 3 — a second reader of `limb_state`

`hittest.py` was the only file permitted to call `view.limb_state(...,
purpose=HIT_TEST_ONLY)`, and `tests/test_limb_state.py` enforced that by
asserting **one reader in one file**. Selection needs the same read for the same
reason: reporting a pick on a limb that is not there is Lobster lying about
geometry, exactly as reporting a hit on it would be.

The file count was never the invariant. Scope §5 and L8 draw the line at
*posing*, not at arithmetic:

> the fix for the old bug (stale hitboxes) shouldn't grow into scope creep
> (Lobster becoming an animation controller).

**Decision: state the property instead of the count.** The guard, renamed
`test_limb_state_is_read_only_to_decide_which_hitboxes_exist`, keeps a named
two-file allowlist (a third reader still fails, and needs an entry here saying
why it asks the hitbox question and not an animation one) and adds the assertion
that actually carries the weight: **neither reader contains `set_pose(` or
`set_root(`.** A file that both reads limb state and poses bones is the exact
collapse L8 forbids, and that is now caught directly rather than by proxy.

The read stays optional in selection: `pick(..., view=view)` respects severed
limbs; without a `view` every rig region is pickable, which is right for a tool
or a test and never reaches a player.

### Two defects the build exposed

Consistent with the renderer: writing a consumer found real bugs in data that
unit tests had not.

1. **Entry-face normals were `(0, 0, 0)`.** The DDA reports the axis it crossed
   on each *step*, but a ray that starts outside the grid enters through a face
   without stepping — so the first voxel, the common case for a wall hit head
   on, had no axis. `_entry_distance` now returns `(t, axis)` from the slab
   test, and the entry face gives the normal.

2. **Every resident cell claimed the ground everywhere.** `ground_height`
   **clamps** at the heightfield edge, which is correct for foot IK and for the
   navmesh recompute — a probe a few centimetres past the last sample should get
   the edge height, not a cliff. It is wrong for *"which terrain is this"*: a ray
   at the ground in the village was answered by the field too, at a clamped
   height, and sorting by distance sometimes picked the wrong one. Added
   `TerrainCollider.covers(x, z)`, a footprint test used only by selection.
   Clamping is unchanged for the callers that want it.

### What this closes and what it does not

- `on_interact` moves from *caller* to **Lobster** in CONTRACT §1. Five of the
  seven Events are now Lobster-raised; `on_item_placed` / `on_item_removed`
  still await §8 item 3, which is a real subsystem and not two missing calls
  (D24's reasoning is unchanged for those).
- Scope §8 items **2 (world-space labels)** and **3 (item representation)** stay
  open and unbuilt. Selection is a prerequisite for both, not a substitute.
- Nothing about damage, interactability or inventory landed here. `chunk_index`
  on a `Selection` is the same index `damage_structure` takes, so a caller can
  turn a pick into a hit — but the turning is the caller's (L8).

---

## D26 — The accelerator seam: pure Python is the reference, native is optional

**Authorised by the project owner**, answering an external review whose leading
finding was that the runtime language is Python and that every hard problem
Lobster solves is a tight numerical loop.

### The measurement, first

The review's numbers were wrong in the arithmetic and right in the conclusion.
`~77` broad candidates is *per arrow*, not 77k; `MAX_BROAD_CANDIDATES_PER_FRAME`
was derived from that as an L6 budget rather than as a meltdown guard; and the
21.4 µs six-bone refinement is gated behind a 3.5 µs body capsule at 1.00
refinements per landed hit, so it is not the per-arrow cost.

None of that rescues the point. Measured end to end — 200 entities in a 128 m
cell, 128 m shots, full `resolve_projectile`:

| | |
|---|---|
| per arrow | **466 µs** |
| arrows per 16.6 ms frame | 36, with nothing else running |
| arrows per 1 ms slice | 2 |

The architecture is fine: cost scales with attacks and tiers, not population,
exactly as §7 claims. **The constant factor is the interpreter.** Against a
C++/Rust implementation this is roughly 50–100×, not the 5–10× the review
estimated.

### The ambiguity

> **L1** — Every hard problem gets the industry's already-solved answer, chosen
> on day one.

> **L7** — Lobster has zero policy. If bloat shows up in a finished build, it
> should be traceable to content, never to the shell.

D6 read L7 as an argument against a dependency tree and chose stdlib-only. D22
already found one place where that preference had been allowed to pick the answer
to a hard problem, and amended it for rendering. This entry is the same question
for arithmetic, and it forces a prior one the documents had never answered:
**is Lobster the shipping runtime, or the executable specification a fast runtime
is built against?**

Resolution order puts cheap contract and zero-policy first, then save/mod
compatibility, then deterministic attributable failure, then performance. A pure
Python shell serves the first three well and fails the fourth. A rewrite serves
the fourth and forfeits the property that anyone can open the geometry layer,
read it, and change it.

### Decision — both, with a seam

1. **The entire public surface and the reference implementation stay pure
   Python**, stdlib only. D6 survives as the rule for the shell. Anyone can open
   Lobster, read how a capsule test or a micro-chunk mesh actually works, and
   change it. That property is worth defending on its own terms and is why this
   is not a staging post on the way to a rewrite.

2. **The hottest loops become replaceable by an optional native module.** When it
   is present it is used; when it is absent the pure-Python path runs and the
   whole suite still passes. This is `select_backend()` (D22) applied to
   arithmetic rather than pixels, and it is what CPython does for itself —
   `_json`, `_pickle` and `_decimal` all have pure-Python twins.

3. **The seam takes a volley, not an arrow.** This is the load-bearing detail and
   it is settled by profiling, not by taste. 400 arrows cost 955,780 Python
   function calls — **2,389 per arrow** — at roughly 60 ns of dispatch each, so
   ~150 µs of the 466 µs is the interpreter calling functions before any
   arithmetic happens.

   | stage | share |
   |---|---|
   | `query_segment` — broad phase | 64% cumulative |
   | `_region_for` — bone refinement | 32% cumulative |
   | `dot` / `sub` / `add` / `quat_rotate` | ~204k calls, pure dispatch |

   Two seams follow, and both are batch-shaped: **broad phase** and **capsule
   refinement**, together 96% of the time.

   - A per-`dot()` or per-capsule seam is worthless: 200,000 FFI crossings per
     volley, each costing about what the Python call cost.
   - A per-*arrow* seam is nearly worthless. Amdahl, at 50× on the seamable 96%:
     `466 × 0.04 + 466 × 0.96 / 50 ≈ 27 µs`, and the 4% of Python orchestration
     that remains is 3.7 ms for 200 arrows — the frame spent on glue.
   - A per-*volley* seam is one call per frame: hand over the index and the whole
     batch, get back results, **release the GIL for the duration**. ~9 µs/arrow ×
     200 ≈ **1.8 ms**. That is the density target, and it is reachable.

   The consequence to state plainly: at that granularity the native module owns
   the *data structures*, not only the loops.

4. **Three conditions, as commitments rather than intentions.** The difference
   between this pattern and theatre is entirely here.

   **(a) Differential testing, not shared test coverage.** Both paths, same
   inputs, asserted agreement, on generated cases as well as fixtures. "Both
   pass the suite" is not the property — the suite can pass on both while they
   disagree on a case no test covers.

   **(b) A stated float tolerance and a stated authority.** Bit-identical is
   probably unachievable: a SIMD path with a different summation order will
   disagree in the last bits, and `_BOUND_EPSILON` exists precisely because an
   exactly-touching bound is decided by float rounding. So the tolerance is
   declared, and one path is named authoritative when they differ. Determinism
   is the reason, and it is the same reason `find_path` breaks ties by id.

   **(c) CI runs both paths.** A native module exercised only on a dev machine is
   D22's failure mode — code shipped that CI has never executed a line of. Being
   the fallback does not make the Python path a substitute for testing the
   native one; it makes the native one easier to forget.

5. **The cost is written down, not discovered.** CONTRACT gains a section at
   the top — it is a property of the whole shell, not of the renderer alone: the
   read-and-modify property is fully true for the reference path and *partly
   false* on the fast path, where editing the Python tells you the truth about
   semantics but does not change what executes. That is an acceptable trade and a
   bad surprise.

### What this settles about Lobster's identity

Lobster is **both** the reference implementation and the runtime, with a declared
seam between them. That is a better answer than either of the two the ambiguity
offered, and it is now recorded rather than implied by the absence of a statement.

### What is not decided, and one warning

- **The native module is not written, and no toolchain, language or binding
  mechanism is chosen here.** Naming those is L1's requirement and is a separate
  entry; this one fixes *where the seam goes* and *what it must prove*.
- **This is the second hard problem answered with a named-but-unwritten native
  path.** D22 named ModernGL and it is still unwritten for want of a GL stack.
  Two is where that stops being a schedule and starts being a pattern. The seams
  must therefore stay narrow and few, and the differential harness — which needs
  no toolchain and can be written today against the pure-Python path alone — is
  built *before* the module, so it cannot become a third unwritten thing.
- **A defect this measurement exposed, independent of the seam:**
  `MAX_BROAD_CANDIDATES_PER_FRAME = 32768` is ~420 arrows, or **~196 ms of wall
  time**. It bounds the algorithm's scaling and not the frame, so it fires long
  after the game has stopped being playable. L6 asks for budgets that are
  enforced; this one satisfies the letter and misses the moment. Tracked
  separately.

---

## D27 — The hit-test budget counted the wrong thing

**Found by measuring for D26**, and fixed on its own terms: this is a defect
whichever way the accelerator seam goes.

### The defect

`MAX_BROAD_CANDIDATES_PER_FRAME = 32768` was described in D16 as "the number
that actually scales in a mass volley, and so the one worth budgeting". Two
things were wrong with it.

**1. The ceiling was ~196 ms of wall time.** At a measured 466 µs per arrow it
permitted about 425 arrows in a frame. It bounded the algorithm's scaling and
not the frame, so it fired roughly twelve frames after the game had stopped
being one. L6 asks for budgets that are enforced; this one satisfied the letter
and missed the moment.

**2. Worse: it charged for a quantity nearly uncorrelated with the cost.**
`buckets_visited` was incremented *after* the empty-bucket check, so it counted
buckets that held somebody — how crowded the cell is, not what the query did.
The grid walk pays for every bucket key it forms and looks up, and on a long ray
through a thin crowd the empty ones are nearly all of them:

> One 120 m shot through an empty cell: **318 µs of work, charged 0 of 32,768.**

You could fire that shot every frame forever and never trip the budget. Lowering
the number would not have fixed this; the budget could not see the cost.

### Ambiguity — what unit should a budget be denominated in?

Resolution order puts deterministic attributable failure (3) above performance
(4) and simplicity (5).

> Failure must be visible and attributable.

Wall-clock time is the honest unit and the wrong one: the same frame would pass
on one machine and fail on another, and a budget that is not reproducible cannot
be a test. Counts are reproducible but, as above, can measure the wrong thing.

**Decision: model the time from the counters.** `modelled_cost_us(scans,
candidates, capsules)` — counters times measured unit costs, no clock read
anywhere. Deterministic, reproducible across machines, and denominated in the
unit a frame is actually spent in.

### The unit costs are derived, not guessed (D20's method)

Least squares over twelve controlled scenarios that vary ray length and crowd
density independently, residual **< 5 µs** on queries costing 18–647 µs, then
validated against real `resolve_projectile` wall time across five densities.

| | measured | shipped | why |
|---|---|---|---|
| per bucket scanned | 1.153 µs | **1.2** | |
| per candidate | 1.930 µs | **2.5** | |
| per capsule test | 3.294 µs *(primitive)* | **12.0** | the counted test *in situ*, not the primitive |

Two notes on that table, because both are places this could have gone wrong.

**The first fit was garbage** — a negative candidate cost and a 104 µs residual
— because `buckets_visited` and `candidates_considered` were collinear by
construction: in a sparse cell every visited bucket yields exactly one
candidate. The regressors only separated once `buckets_scanned` existed. The bad
fit is what exposed the defect.

**The capsule constant is 3.6× the primitive.** Validating the model against real
wall time showed it under-predicting exactly where capsule tests dominated, and
under-prediction is the wrong direction for a budget. A "capsule test" as
counted carries its share of the refinement path around it — bone matrices,
region selection — so the constant is the amortised in-situ cost. Everything is
rounded **up** from the fit so the model over-predicts across the whole sampled
range (measured ratios 1.02–1.16). A budget that errs must err towards tripping
early, for the same reason `_BOUND_EPSILON` errs outward.

### The slice, and the number it produces

`HIT_TEST_FRAME_BUDGET_US = 2000` — one cell's share of a 16.6 ms frame.
Hit-testing shares the frame with rendering, animation, AI, audio and physics;
~12% for one system in one cell is generous. **Per cell** because L6 says budgets
are declared and enforced per cell: a game with attacks in nine resident cells
should see nine violations, not one blurred total.

That buys **about four 120 m arrows into a 200-strong army, per cell, per
frame.** The number is alarming and it is what the measurement says. Raising the
budget to make it look better would be the same dishonesty this entry exists to
remove; the remedy for the *capability* is D26's accelerator, not a bigger
constant.

### The violation names the driver, because the remedy depends on it

The old message advised the zone-occupancy path (§6.5/§7) for every violation.
Once the walk was counted, that advice turned out to be wrong in the more common
case: a 200-strong army spread over open ground trips at 4 arrows on **grid
traversal**, where there is no crowd to resolve by occupancy. A packed 400-body
crowd trips at 1 arrow on **broad candidates**, where the old advice is right.

So the violation picks the dominant term and matches the remedy to it — crowd
cost gets the occupancy path, walk cost gets "shorten the segment, or use the
volley seam". Attribution that names the wrong fix is not attribution.

### What changed

1. `QueryStats.buckets_scanned` — every key formed and looked up.
   `buckets_visited` stays, meaning what it always did (occupancy, and the input
   to any future BVH argument); the two are now separate because they answer
   different questions.
2. The budget is `frame_budget_us`, enforced against the model. The three unit
   ceilings are derived from the same slice and kept as secondary guards, each
   being the point where that unit alone spends it — "166 capsule tests spends
   your slice" is actionable in a way "2 ms" is not.
3. `tests/test_projectile_snapshot.py` — one test asserted twelve arrows were
   "nowhere near the ceiling". They were nowhere near the *old* ceiling and
   ~2.5× over the frame; the test encoded the defect. Replaced with the honest
   assertion plus a regression guard that fires if anyone goes back to charging
   only occupied buckets.

### Not done, and deliberately

`query_segment` computes `span = int(radius / cell_size) + 1`, which for a 2.5 m
radius on a 2.5 m grid scans a 5×5 neighbourhood per step where 3×3 would do —
plausibly a large share of the traversal cost. That is a real optimisation and
it is **not** part of this fix. Folding a performance change into a correctness
fix would mean neither could be measured, and the budget has to be trustworthy
before anything is tuned against it. Logged for its own entry.

---

## D28 — The differential harness, built before the module

**D26's first commitment, delivered.** It is deliberately first:

> the differential harness — which needs no toolchain and can be written today
> against the pure-Python path alone — is built *before* the module, so it
> cannot become a third unwritten thing.

`lobster/conformance.py`, `tests/test_conformance.py`, `conformance/vectors.json`,
`python -m lobster.cli conformance`.

### The seam is two kernels, and one had to be extracted to become one

| kernel | question |
|---|---|
| `segment_query` | which entities lie within `radius` of this segment |
| `nearest_region` | which bone a ray struck, over an already-filtered capsule list |

`nearest_region` was the body of `HitTester._region_for_ray`. **A method body
cannot be swapped out; a module-level function can** — so it is one now, and a
test asserts it stays one. That is what "fixing where the seam goes" (D26) means
in code rather than in prose.

**Both kernels sit below the `limb_state` read.** The caller does the live query
and hands over only the capsules that survived it. A native kernel therefore
never touches limb state, the L8 boundary stays in Python permanently where
`tests/test_limb_state.py` can see it, and the allowlist stays at two files.

### Ambiguity — what does "agree" mean?

> **(b) A stated float tolerance and a stated authority.** Bit-identical is
> probably unachievable.

Demanding bit-identity would fail every honest port; loosening until the suite
passes would prove nothing. So the rules are declared:

| property | rule |
|---|---|
| membership | **exact**, except a candidate within tolerance of the radius, which is admissible either way |
| distance | within `DISTANCE_TOLERANCE_M = 1e-4` (0.1 mm) |
| order | free where two distances are within tolerance, fixed everywhere else |
| region | free where the two nearest bones are within tolerance, fixed everywhere else |
| `precise` | exact, except a grazing case whose best gap is within tolerance of zero |

**Authority: the pure-Python path.** Inside tolerance its answer is the specified
one; outside tolerance the other path is wrong.

1e-4 m is wide enough for an f32 native kernel and far below anything a player or
a damage number can distinguish. The boundary carve-outs are the `_BOUND_EPSILON`
hazard again: a value sitting exactly on a threshold is decided by rounding, and
neither side of it is a defect.

**A tie-break that is an artefact, and is now declared as one.** `nearest_region`
compares with strict `<`, so equidistant bones resolve to whichever capsule comes
first in the list. That is iteration order, not geometry. Rather than change it —
a behaviour change smuggled into a harness — the kernel's contract states that
capsule order is part of the input, and the comparator treats symmetric ties as
free.

### Why it is not vacuous with one implementation

Comparing Python to Python passes forever. Three things make it real today:

1. **The comparator is mutation-tested**, and this is the bulk of the suite.
   Dropped candidate, spurious candidate, distance nudged past tolerance,
   reordered pair, renamed region, flipped `precise` — each is injected and
   must be caught. The latitudes get the same treatment in reverse: a
   sub-tolerance nudge, a boundary candidate, a symmetric tie must each be
   *allowed*. A harness that catches everything would fail every honest port;
   one that catches nothing is decoration.
2. **Golden vectors are committed** (80 cases), so the Python path is pinned. If
   a geometry change moves an answer this fails, and regenerating the vectors
   becomes a deliberate act rather than a silent one.
3. **Determinism and order-independence are asserted on the reference** — same
   input twice, and shuffled insertion order, must give identical answers. Both
   are properties a native kernel must also satisfy, and both are checkable now.

A fourth guard watches the generator itself: it must produce enough non-empty
results, multi-hit results, empty results and distinct regions, or every test
above quietly becomes vacuous.

### Two things the build found

**The first generator was a bad suite.** 35 of 40 segment cases came back with no
hits at all — it exercised the grid walk and nothing else, leaving membership,
ordering and distance essentially untested. Two causes: rays aimed at random
points rather than through the crowd, and radii as low as 0.5 m when entities sit
at `y=0` and shots at `y=1.2`, so no XZ layout could ever produce a hit. Now 22
of 40 return candidates, 11 return two or more, and 18 are deliberate misses.

**Invariant 1 caught the harness writing a file.**
`test_no_private_save_format_anywhere` forbids anything in `lobster/` from
opening a file for writing, and the first draft of `write_vectors` did. The
invariant is right and a carve-out would have been the wrong fix, so the harness
now *produces* the bytes and the caller persists them: `dump_vectors()` returns
text, and the CLI prints to stdout for redirection. The same guard then caught
the CLI flag doing it too. Serialisation is data; persistence is somebody else's
decision (L5).

### Still owed from D26

**(c) CI runs both paths** cannot be discharged until a second path exists. What
exists now is the harness it will be run through, and `lobster conformance`
exits non-zero on any divergence, so wiring it into CI is a one-line job the day
a native kernel lands.

---

## D29 — Three render tiers, and the chain is reported (amends D22)

**Directed by the project owner.** D22 named ModernGL and left one fallback:
GPU, else the pure-Python rasteriser. That skips the tier most development
actually happens on.

| tier | what it is | expectation, stated so nobody benchmarks the wrong one |
|---|---|---|
| `moderngl` | OpenGL 3.3+ on a real GPU | the default and the target — 60 FPS on a 128 m cell with destructible geometry, and the battery-efficient path on a phone |
| `moderngl-llvmpipe` | the same GL code on Mesa's software rasteriser | fast enough for development and many tests. **Not player-facing** |
| `software` | the pure-Python rasteriser | correct pixels for a screenshot, a debug overlay, a CI oracle. Slow, viable everywhere |

### Ambiguity — is llvmpipe a backend or a driver?

It is a driver, and that decides the implementation. A GL context on llvmpipe
runs *the same ModernGL code* as one on a discrete GPU; nothing about the
renderer changes. So the two GL tiers share a class and are told apart only by
what `GL_RENDERER` reports.

**But they are reported as two tiers anyway**, because their performance is two
orders of magnitude apart. Folding them into one "moderngl: available" line
would satisfy the code and mislead the reader, which is the failure mode D22 and
D24 were both about. Resolution order puts deterministic attributable failure
above implementation simplicity.

### Decisions

1. **Preference order is hardware GL → software GL → pure Python**, and the
   first available wins.
2. **The middle tier is optional and never vendored.** Mesa is an OS package.
   Lobster neither ships it, requires it, nor errors when it is missing — the
   chain simply drops through.
3. **The bottom tier stays viable.** Some CI images have Mesa and some do not;
   if the zero-external-package path ever stopped working, CI would become
   environment-dependent, which is a worse problem than a slow rasteriser.
4. **Selection returns the chain, not just the answer.** `select_backend()`
   attaches a `SelectionReport` — `chosen`, `skipped` as `(name, reason)`
   pairs, and `summary()` giving
   `moderngl unavailable -> moderngl-llvmpipe unavailable -> software`.
   `lobster contract` prints it under `render_backend_selected`. Silent fallback
   is how somebody spends an afternoon profiling the wrong layer.
5. **Every tier that cannot run says why, in every branch.** Asserted, rather
   than left to the branch nobody exercises.
6. **`select_backend(name)` still raises** for a tier that cannot run. Unchanged
   from D22 and for the same reason.

### The part that could not be executed here, and how that was contained

This machine has no graphics stack at all, so `probe()` can only ever take the
"no GL" branch. Writing a three-way chain that has only ever run one way is
precisely the D22 mistake.

So the driver query is split from the tier logic. `gl_renderer_string()` is four
lines that talk to a driver — the `import moderngl` half executes here (it
raises), the context half is marked no-cover rather than pretended about.
Everything else takes the renderer string **as an argument**: `probe_with(None)`,
`probe_with("llvmpipe (LLVM 15.0.7, 256 bits)")`,
`probe_with("NVIDIA GeForce RTX 4070")` all run in the suite, including the
`softpipe` / `swrast` / `Software Rasterizer` spellings, and a test asserts a
reported renderer string is always quoted back in the detail so a misdetection
is debuggable from a log alone.

**The GPU backend itself remains unwritten** (D22), so both GL tiers currently
report unavailable-because-unimplemented while correctly identifying which tier
the machine *would* land on. The detection and the implementation are separate
facts and are now reported separately.

---

## D30 — The load-bearing "false negative" was not one; the coupling behind it was real

**A correction to my own claim.** Reviewing Shrimp's finding 3C I wrote that the
build-step inference had a genuine false negative — "a pillar 5 m below a bridge
deck is not flagged, so destroying it leaves the deck floating and still
walkable" — and listed it as an outstanding defect to fix.

**Tested, and it does not exist.** Two things were wrong with the claim:

1. **The arithmetic.** `NavPoly.bounds()` reaches 0.5 m below the surface;
   `_poly_walkable` looks for support 0.35 m below it. The inference's reach
   already covers the probe, so every chunk that can change walkability
   intersects a polygon bound and is flagged. Swept over both directions in
   `tests/test_navmesh_agreement.py`, at 24 depths and 24 heights.
2. **The example.** Destroying a chunk removes *that chunk*. There is no
   structural-collapse cascade — that would be damage simulation, which is not
   Lobster's (L8, §0 non-goals) — so the deck stays supported by its own voxels
   and its walkability genuinely does not change. The scenario described a
   physics feature that does not exist and called its absence an inference bug.

### What was actually wrong

The two numbers were **coupled by nothing but the arithmetic happening to work
out.** Nothing named the relationship, nothing enforced it, and nothing would
have noticed it breaking. Raise `DEFAULT_SUPPORT_PROBE_M` past 0.5 — a plausible
tuning change for taller terrain steps — and a chunk sitting between the probe
and the bound would hold a polygon up while intersecting nothing. Destroy it and
walkability changes with no recompute queued: a silent false negative, which is
the precise failure Scope 15.7 says must not come back.

**Decision: make the relationship structural.**
`LOAD_BEARING_PROBE_MARGIN_M = 0.5` is now a named constant that `bounds()`
uses, documented as *never* permitted below `DEFAULT_SUPPORT_PROBE_M` and
deliberately larger, because the inference must err towards over-flagging — one
recompute that changes nothing is the cheap direction. Four tests pin it,
including a sweep and a check that over-flagging still stops somewhere: a bound
that swallowed the whole cell would queue a recompute for destroying anything.

Verified by mutation — dropping the margin to 0.2 fails two tests with the
reason spelled out, rather than passing quietly.

### The general point, recorded because it will recur

A defect asserted from reading code is a hypothesis. This one survived a review,
a written finding and my own restatement of it as outstanding work, and died in
about a minute against an actual test. The rule that keeps paying: **measure or
reproduce before changing anything** — including when the thing being changed is
something I claimed myself.

---

## D31 — The grid dilation was one bucket too wide (deferred from D27)

D27 logged this and refused to do it there:

> Folding a performance change into a correctness fix would mean neither could
> be measured, and the budget has to be trustworthy before anything is tuned
> against it.

The budget is now trustworthy, so here it is.

### The defect

`query_sphere` and `query_segment` both dilated every touched bucket by
`span = int(radius / cell_size) + 1`. The tight bound is different: two bucket
indices differ by `d` only if `(d - 1) * cell < r`, so `d < r/cell + 1` and the
correct span is **`ceil(r / cell)`**.

Those agree everywhere except when `r / cell` is an **integer** — the classic
`floor + 1` where `ceil` was meant. And that is precisely the shipping case:
`broad_margin()` floors at `PROJECTILE_BROAD_RADIUS_M` (2.5 m) and the grid is
`SPATIAL_GRID_CELL_M` (2.5 m), so the ratio is exactly 1.0 and every step
scanned a **5×5 neighbourhood where 3×3 was sufficient** — 25 bucket lookups
instead of 9.

### How it was made safe to change

This alters which entities the broad phase *finds*, so it is a correctness
change wearing a performance change's clothes. Three independent checks:

1. **Brute force as the oracle.** 8,000 randomised queries across 5 crowd sizes,
   3 cell sizes, 6 radii and both query shapes, compared against a direct
   distance test over every entity. Zero mismatches before the change (the
   baseline) and zero after.
2. **The conformance vectors did not move.** D28's 80 committed cases still
   compare clean, so no answer changed anywhere the harness reaches. This is the
   first time that harness has earned its keep on something other than itself.
3. The whole suite, unchanged.

`_span` is floored at 1 because `_segment_buckets` samples the line rather than
supercovering it, so a diagonal can cross a bucket no sample lands in.
Consecutive samples are at most one bucket apart, so a span of 1 catches it.

### What it bought, and what it broke

| | before | after |
|---|---|---|
| bucket scans per 128 m arrow | 280 | **162** |
| cost per arrow (200 bodies) | 514 µs | **374 µs** |
| arrows per cell per frame | 4 | **6** |

**And it decalibrated the frame budget**, which is the part worth recording.
D27's fit had no per-query term — it came back slightly negative and was
dropped, because at ~280 scans per query the fixed setup cost hid inside the
scan coefficient. At 162 scans it no longer does, and the model went from
over-predicting by 2–16% to **under-predicting by up to 43%** — the wrong
direction for a budget, and a silent restoration of exactly the class of defect
D27 existed to remove.

So the unit costs were re-derived on the new code, four terms this time, over
nine scenarios spanning crowd size and ray length. `PER_QUERY_US = 10.0` is now
explicit, so a future change to the scan count cannot quietly decalibrate the
budget again. Each coefficient is rounded up from its own fitted value rather
than compensated for across terms; the model over-predicts everywhere in the
sampled range (ratios 1.02–1.13).

**The lesson is the one D27 already stated and this confirmed:** a cost model
calibrated on one counter mix is not valid on another. Optimising the thing the
budget counts obliges you to re-derive the budget. Doing these two changes in
one commit would have hidden that entirely.

---

## D32 — Fast travel releases before it loads (Shrimp finding #6)

**Their ask, with the shape left to Lobster.** Reproduced first, on a purpose-built
world of two 3×3 exterior clusters, because the standard fixture has two
exterior cells and they are neighbours:

```
walked into cell-home-1-1: 5 resident
fast travel to cell-far-1-1 FAILED:
  cell 'cell-far-1-0': exceeds 'max_resident_cells': 10 > 9 (during charge)
```

The finding's sharpest observation is in that last line: the violation names
`cell-far-1-0`, which has nothing to do with either endpoint. Whoever hit this
first would have gone looking at the wrong cell.

**The arithmetic is structural.** A 4-connected ring is 5 cells; two rings that
share nothing sum to 10 against a ceiling of 9. Fast travel is a documented
first-class entry path — `default_spawn_transform` is defined as *"where an
entry that did not come through an authored connection arrives — fast travel,
first-time dungeon entry"* — so this is not an exotic case.

Shrimp offered two remedies and preferred the second: raise the ceiling to
`2 × max_ring`, or skip the load-then-unload overlap on a discontinuous move.

**Decision: the second.** Raising the ceiling would make it mean less — 9 was
derived to bound a *walk*, and doubling it to accommodate a case that does not
need the memory at all would stop it catching the case it was for. A jump has no
continuity to preserve.

### The ambiguity: what is a jump?

Shrimp's rule was "the target is not already resident". **That is wrong, and the
first version of this fix shipped it and broke a test that caught it
immediately.**

An interior is never in an exterior's ring (D8: only exteriors preload). So
"not resident" classifies **every walk through a keep door** as a teleport, which
silently deletes the transition peak that `MAX_TRANSITION_PEAK_BYTES` exists to
bound — trading one budget defect for another, quieter one.

The discriminator is not residency, it is the **authored connection**, and Scope
names it in the definition of `default_spawn_transform` quoted above. A move is
continuous when:

1. the destination is already resident — you can see where you are going, or
2. an authored connection runs there from where the player stands — a door, a
   path, even into a room that was never preloaded.

Everything else is an entry that did not come through a connection, which is
exactly the set Scope calls fast travel and first-time dungeon entry.

`is_continuous_move(view, cell_id, from_location_id=None)` is **public**, because
the answer is a fact about residency a caller may want before it commits: it is
the question "does this need a loading screen". Lobster reports it and decides
nothing about it (L4). `from_location_id` overrides the manager's own last
position, since a caller saying what it came through is more authoritative.

### Consequences

- Cells in both sets are untouched either way. `load` is idempotent and `unload`
  only takes cells outside `desired`, so a jump whose rings happen to overlap
  keeps the overlap rather than churning it.
- **Across a jump, `on_exit_cell` now precedes `on_enter_cell`.** §13 fixes the
  Events, not their order, and for a teleport this is the truer sequence anyway.
  A walk is unchanged. Documented rather than left to be discovered.
- Shrimp's `Scene.enter` workaround can go: the behaviour is in the layer that
  owns residency.

### Verified in both directions

Mutation, because a rule with two branches needs both pinned. Forcing
`walked = True` (the old behaviour) fails the two fast-travel tests; forcing
`walked = False` fails the two transition-peak tests. Neither half can be
deleted without the suite saying so.

---

## D33 — `Item.world_transform`, and the co-null invariant (Scope §8, step 1)

**Approved by the project owner** off the §8 proposal.

Scope §8 gives Lobster *"physically placing/removing an item's representation in
the world"* and gives Octopus *"inventory data — `Item` records"*. So the item is
Octopus's; **where it is** is Lobster's, and that has to survive a save (L5:
Lobster saves nothing of its own).

**Decision: a field on the existing `Item` record**, declared by
`packages/lobster_geometry.json` through `schema_extensions.new_fields` — the
same mechanism that already declares `Location.default_spawn_transform`,
`lobster_budget`, `sound_sources`, `spatial_grid_cell_m`, `exterior_grid` and
`Zone.shape`. `REPLACE`, because an item is in one place and two mods that both
move the same sword must not average it.

Rejected: a new `ItemPlacement` record. Strictly more contract surface for no
gain, and splitting placement across two records invites them to disagree about
where a sword is.

### The co-null invariant

> `world_transform` and `current_location_ref` are co-null. Either an item is in
> the world and has **both**, or it is not in the world and has **neither**.

Octopus settles the semantics: `StdLib.location_of` maps an Item to
`current_location_ref`, so a location *is* the claim "this is out in the world";
and `world_transform` is cell-local, so a transform alone names a position in no
coordinate system at all — the same defect shape as a Zone shape with no
`shape_location_ref` (D14).

**In both directions the symptom is identical: the item silently never appears.**
That is why both are build errors — `item_transform_without_location` and
`item_location_without_transform` — rather than one being tolerated.

An earlier draft checked only one direction and asserted that a location with no
transform was clean, on the reasoning "it must be an inventory item". The project
owner called this out mid-build and was right: Octopus's own `location_of` says
otherwise.

### `save_layer_only`, and a conflict flagged rather than papered over

`world_transform` is deliberately **not** save-layer-only, mirroring
`destroyed_chunks` (D3): a mod may ship a pre-ruined keep, so by the same
argument it may ship a sword already lying on a table.

**But Octopus declares `Item.current_location_ref` as `save_layer_only`**, so a
content package cannot legally supply the other half. A pre-placed item is
therefore impossible today regardless of what Lobster does. Rather than silently
match Octopus's restriction — which would bury it — the lint fires
`item_transform_without_location` and names `save_layer_only` in the message, so
an author who tries learns why in one read. **This is an Octopus decision to
revisit and is recorded here as an open question for that repo.**

---

## D34 — `items_in_location` is an eighth permitted query (Scope §8, step 2)

`CellManager.place_item` / `remove_item`, `lobster/items.py`, and the query that
makes a resident cell able to say what is lying about in it.

### The contract change, argued rather than assumed

§13's list of permitted live queries had seven entries and a test pinning it
exactly. This adds an eighth, and the guard failed on the way — which is what it
is for.

> **Live queries Lobster is permitted to call** […] and — **new in v0.3, closing
> the §6.5 gap** — `queries.structures_in_zone(zone_id)` and
> `queries.structures_in_location(location_id)`, both pure lookups over resolved
> state, same cost model as the occupancy query.

**The list is closed, not frozen**, and it was extended once before on exactly
these grounds. §8 assigns Lobster item representation in the world, and a cell
cannot represent the items in it without asking which those are. Same shape of
read, same cost model: O(items in that location), not O(items in the world), via
a derived index rebuilt when the resolution changes and never persisted.

Alternative rejected: have the caller place every item on load. The save already
records where each item is; making Shrimp enumerate them and call `place_item`
would mean re-writing the records it just read, on every load.

`tests/test_contract.py` now asserts the count is eight and that the reason is
recorded, so the *next* addition is also an argued one.

### Two fields, no atomic write

**Octopus has no multi-field write.** `PATCH` takes one `field` and one `value`
(`lce/ops.py`: CREATE, PATCH, MERGE, DELETE, DELETE_ENTRY). The first draft of
this module invented a `PATCH_MANY` and claimed atomicity for it; checking
`ops.py` rather than assuming is what caught it.

So the co-null invariant is held by **ordering**, and the honest claim is
weaker but true: *every intermediate state is "not in the world."*

| | order | intermediate state |
|---|---|---|
| place | `world_transform`, then `current_location_ref` | a position in no cell — not indexed, not drawn, not pickable |
| remove | `current_location_ref`, then `world_transform` | same — out of the index on the first write |

`current_location_ref` is the **commit point** in both directions, because the
index keys on it. A crash between the two writes leaves a half-placed record,
which the index skips at runtime and the lint rejects on the next build: loud on
inspection, inert in play. All four ops are validated by Octopus's own
`validate_op_shape` in the suite, so an invented op cannot recur.

### What this closes, and what it refuses

**All seven contract Events are now Lobster-raised.** D24 recorded three that
were declared and never fired; D25 closed `on_interact`, and these two close the
rest. The pinning test now asserts the awaiting set is empty, so an Event cannot
go back to being a promise nobody keeps.

Refused, and asserted structurally: no pickup, no inventory, no weight, no stack
count, no reachability, no owner. `on_item_removed` reports the cell and
transform the item **had**, not where it went — Lobster does not know whether it
was picked up, destroyed or teleported, and guessing would be policy (L4).
`model_ref` is passed through untouched; there is no asset pipeline, and
inventing one under the heading "place an item's representation" would be the
scope creep L8 forbids.

Placing into a non-resident cell **raises**. The transform is in that cell's
coordinates, so a caller placing into a cell Lobster cannot see is describing a
position it cannot check against terrain, structures or the cell's extent.
Removing something that is not in the world returns `None` rather than raising:
two systems racing for the same sword is ordinary, and the loser should get a
null.

---

## D35 — D33's open question, closed by Octopus (two-field placement)

**Resolved upstream.** D33 recorded that `world_transform` was deliberately
content-settable while Octopus's `current_location_ref` was `save_layer_only`,
so a mod could not ship a sword on a table, and flagged it as an Octopus
decision to revisit. Octopus revisited it (their D52) and added
`default_location_ref` in the content layer, with `current_location_ref` still
save-only and still winning when set.

Their note back is worth keeping, because it is the argument for the whole
habit:

> **Lobster's lint was right to name the restriction.** Its message told an
> author *why* placement was refused instead of silently matching the rule.

A lint that had quietly matched the restriction would have taught every content
author "items cannot be placed" as a fact about the engine. **D33's open
question is closed.**

### What changed here

**1. The resolver, not the fields.** Octopus D52 introduces
`queries.item_location` and observes that three call sites read the raw fields
and a fourth "would have had to remember the fallback". Lobster is that fourth
site, so `octopus_bridge.resolve_item_location` delegates to theirs and nothing
in `lobster/` re-derives `current or default`.

That was not a hypothetical. The first pass fixed `build_item_index` and left
`PlacedItem.from_record` and `CellManager.remove_item` reading the raw field, and
a mod-placed sword **raised instead of appearing**. A test now walks `lobster/`
for raw reads of the fallback field and fails on any, so the fourth site cannot
quietly become a fifth.

**2. The lint permits content placement and still refuses the save field.**
`item_current_location_in_content` fires when a content package sets
`current_location_ref` - detectable because the build resolves through
`content_view`, which has no save layer, so anything seen there came from
content. The message keeps naming `save_layer_only` and **now names the remedy
it had none for before**.

**3. A new check falls out of Octopus's model.** *"Carried by X is just an item
located at X"*, so an item whose location resolves to a Character has no
business also having a `world_transform`: it would be in a pack and lying on the
floor at once. `item_placed_on_a_character`.

**4. `remove_item` was wrong, and this is a correction.** It cleared the
location as well as the transform. Both halves of that were mistakes once
Octopus's model was read properly:

- **It cannot work.** Nulling `current_location_ref` falls back to
  `default_location_ref`, so removing a mod-placed sword would *restore* it to
  its table.
- **It is not Lobster's to say.** The location field is where the item **went**,
  and Lobster does not know whether that is a backpack, a chest or nowhere.
  Writing it would be guessing, which is policy (L4).

Removal now clears `world_transform` alone. That is sufficient - the index keys
presence on the transform, so the representation leaves the world on that write
whatever any location says - and it is honest about the limit of what Lobster
knows. The caller records where it went.

### The invariant, restated

D33 called `world_transform` and `current_location_ref` co-null. With two
placement fields the accurate statement is:

> An item is in the world when it has **a resolved location and a
> `world_transform`**. With neither it is nowhere. With a location and no
> transform it is in somebody's pack, or half-written - either way it has no
> representation.

The co-null shape survives; what changed is that "location" now means the
resolved answer rather than one field. The runtime index keys presence on the
transform, which is why a save can legitimately hold "located, not manifested"
while the *content* lint still rejects it - the lint is an authoring check, run
on a content-only resolution.

---

## D36 — `ITEM` is a fifth selection kind, and it is budgeted (Scope §8, step 3)

**Approved by the project owner** as decisions 2 and 4 of the §8 proposal.

### Not a prop

`SELECTION_KINDS` was closed at four and pinned by a test, so a fifth is a
contract change. It is the right one: `PropPlacement` has said so since it was
written —

> Anything the player can pick up, open or be told about is an Octopus `Item`
> record placed through the ordinary path […] it does not live here.

Collapsing them would erase a line the code already drew, and `target_id` would
stop being a record id a caller can resolve. Items also need a `view`, because
they live in records rather than in the bundle; a caller passing none is asking
about baked geometry and correctly gets no items.

### The budget, and the number it produced

The owner's condition was explicit: *"Items must participate in the same
selection cost accounting as props so a cell full of dropped loot cannot
silently blow the per-frame pick budget."*

Measured first, as always. 25 → 200 items in one cell, fitted on the slope:

| | µs per item per pick |
|---|---|
| first implementation | **8.28** |
| after the fix below | **4.15** |

**Half of that cost was construction, not geometry.** `_items` called
`placed_items`, which builds a `PlacedItem` — and a `Transform`, and a rotation
quaternion — for *every* item in the cell, when the capsule test needs a
position and nothing else. On a typical frame the ray misses all of them, so
that was full construction cost paid entirely for misses. It now tests raw
records and constructs nothing until something hits.

That is D31's lesson applied again: **fix the cost before budgeting it**, or the
budget enshrines the inefficiency.

Then the ceiling, derived rather than chosen (D20's method):

```
MAX_ITEMS_PER_CELL = SELECTION_PICK_BUDGET_US
                     / (PER_PICKED_ITEM_US * MAX_RESIDENT_CELLS)
                   = 1000 / (4.2 * 9)
                   = 26
```

`SELECTION_PICK_BUDGET_US = 1000` — about 6% of a 60 FPS frame for a
once-per-frame crosshair question. Dividing by `MAX_RESIDENT_CELLS` is not
pessimism: a pick tests every item in **every** resident cell, so the worst case
is all nine full at once.

### 26 is low, and that is the honest number

Item picking has **no broad phase.** Entities are in the cell's `SpatialIndex`
and get a grid walk; items are not, because their positions live in Octopus
records rather than in a structure Lobster owns and keeps in sync.

So the scan is linear, and the ceiling reflects it. **If content needs hundreds
of dropped items per cell, the fix is to index them, not to raise this number** —
raising it moves the cost from a loud refusal at placement time to a silent
millisecond every frame, which is precisely the trade L6 exists to forbid. Logged
as the remedy rather than done now: indexing items means keeping an index in step
with records that anything may write, which is a real design question and not a
tuning knob.

### Enforcement

`place_item` checks `max_items` **before** writing, so a refusal leaves no
half-placed record and fires no phantom `on_item_placed`. Moving an item already
in the cell is not a new item, or a full cell could never rearrange itself.

The ceiling is per cell (L6), declarable in `lobster_budget`, and inherits the
existing discipline for free: a cell may declare a **lower** ceiling than the
shell default, never a higher one.

---

## D37 — World-space labels, and items in the draw list (Scope §8, steps 4–5)

**Approved by the project owner** as decision 3 of the §8 proposal, with the
instruction: *"Keep it pure: no text, no 'should draw', no filtering by
gameplay importance."*

`lobster/labels.py`, `LabelAnchor`, `label_anchors(camera, selector, targets)`.
Scope §8 is now complete.

### What a label is, reduced to geometry

Four numbers and a boolean: a world anchor, its screen projection, the distance,
and whether anything stands in the way. Everything else was refused, and each
refusal is asserted rather than described:

| not owned | why |
|---|---|
| the text | Lobster has never known a display name, and §5 invariant 3 forbids it naming a creature or an item at all |
| whether to draw | distance falloff, "only show hostiles", "hide during menus" - content deciding what matters (L4) |
| priority | nearest is the only order geometry supports, exactly as in selection (D25) |
| declutter | two labels on one pixel is a layout problem, and layout is 2D chrome |

**On declutter specifically.** The proposal offered to report screen-space
overlap and let Shrimp decide what to drop. Read against *"keep it pure"* that
is still one step onto the slope, so it is not built: `screen_point` is handed
over and Shrimp can compute overlap in two lines. A test asserts two anchors on
the same pixel come back as two anchors.

### Occlusion reuses `Selector`, and that is the design

A label is hidden when something stands between the camera and its anchor -
which is the question selection already answers. Sharing it buys a property
worth more than the code saved: **labels and picking can never disagree.** If
the crosshair says a wall is in the way, the label behind it cannot claim
otherwise. Asserted directly.

Two details that would have been bugs:

* **A label is never hidden by its own subject.** The anchor floats just above
  the thing it labels, so a ray reaching it clips the very shoulder it hangs
  over. The occlusion test ignores hits on the target itself.
* **`OCCLUSION_EPSILON_M` errs towards visible**, the same reasoning as
  `_BOUND_EPSILON`: a surface exactly at the anchor is decided by rounding, and
  a label that flickers off when you look straight at a thing is worse than one
  that lingers a frame.

### Anchors are derived from the rig, not from an assumed humanoid

An entity's anchor sits above `whole_body_capsule()`, so a label over a spider
is over the spider - the same argument that made the projectile gate rig-derived
(D19). A structure anchors above its **top**, not its centroid, because a
centroid anchor is inside the gatehouse and therefore behind its own wall on
every approach.

`screen_point` is `None` only for an anchor at or behind the near plane. That is
deliberately **not** the same as off screen: an off-screen anchor projects fine
and returns coordinates outside the viewport, which is what an edge-of-screen
marker needs.

A target that is in no resident cell is **omitted, not raised**. An NPC walking
out of the resident set is ordinary, and raising would make a caller handle an
exception on a normal frame.

### Step 5 — items in the draw list

`ITEM` joins `DRAW_KINDS`, and `build_draw_list` takes a `view`.

**Items need it and props do not**, which is the whole difference: a prop is
baked into the bundle, an item is an `Item` record read live. `render_resident`
forwards its view, and a test asserts it does - **without that forwarding a
dropped sword would be pickable, labellable and invisible**, which is precisely
the silent disagreement between subsystems that §13 invariant 2 exists to
prevent. A caller drawing baked geometry alone (a build-step preview) passes no
view and correctly gets no items.

`ITEM_DRAW_RADIUS_M` (0.6) is deliberately **larger** than
`selection.ITEM_PICK_RADIUS_M` (0.35). Both are invented - items declare no
extent, because `model_ref` resolves to nothing until there is an asset pipeline
- but they err in opposite directions on purpose: culling something that turns
out to be invisible costs one wasted draw, while culling something visible is a
missing sword. **A cull must err towards drawing.**

### What Scope §8 still does not do

Items reach the draw list as bounded entries with no mesh, exactly where props
have always been. **There is still no asset pipeline**, so nothing resolves
`model_ref` into geometry. That was stated when step 2 landed and it remains
true: inventing one under the heading "place an item's representation" would be
the scope creep L8 forbids, and it is a separate decision about asset loading
that nobody has asked for yet.

---

## D38 — A broad phase for items, so the ceiling could rise

**Asked for directly**: *"Index items in the SpatialIndex so the ceiling can
rise."* D36 had logged the remedy and declined to build it, so this is that
entry being cashed.

### Not in `SpatialIndex`, and the measurement is why

`SpatialIndex` carries two things items have no use for:

* **A tier.** `check_tier` validates against the entity vocabulary, and D18 is
  explicit that a tier is a query answer about *entities*. Items would have to
  borrow one, and a mis-scoped query could then hand an item to
  `resolve_projectile`, where `_require_rig` raises.
* **Snapshot provenance.** `snapshot_seq` and `snapshot_reason` exist so a
  stale dormant entity's position is attributable (Scope §15.10). **Items
  cannot go stale**: the grid is derived from the resolution and discarded when
  the resolution changes, so there is no staleness to attribute.

Measured, that machinery costs **2,537 µs against 527** to index 1,000 items —
5× the build, to answer questions items never ask.

**But the traversal is shared.** `ItemGrid` and `SpatialIndex` both walk through
`spatial.dilated_segment_walk`, because the walk is the part that has been wrong
twice — D16's sphere bound and D31's off-by-one dilation — and two copies would
mean fixing it twice. That is the reviewer's duplicated-mesher objection (3A)
taken seriously in a case where no law forces the split.

### What the measurements decided

Three of them changed the design rather than confirming it.

**1. A coarser grid is worse, not better.** The obvious tune — bigger buckets
for sparse items — loses badly, because a dilated corridor at 16 m sweeps most
of the cell and tests nearly everything. 2.5 m, matching the entity grid, won at
every item count.

**2. The win depends on ray length, not item count.** The first benchmark used
a 120 m ray, which is the worst case for a walk and the best case for a scan,
and made the grid look marginal. At interaction range it is not marginal:

| reach | items | linear | grid |
|---|---|---|---|
| 3 m | 1,000 | 4,637 µs | **52 µs** (89×) |
| 20 m | 1,000 | 4,289 µs | **65 µs** (66×) |
| 120 m | 25 | 108 µs | 212 µs (**0.5×**) |

So the grid is not used unconditionally. `ItemGrid.walk_cost` reports what the
walk would cost and `_items` takes the scan when the walk is worse — a long ray
through a thin scatter. A cell with no items skips both, which matters because
most resident cells hold none and an empty grid still pays a full walk.

**3. The grid must be cached per *resolution*, not per view.** `frame()` builds
a new `FrameView` every frame even when nothing was written, so caching there
rebuilt an unchanged grid once a frame — the exact cost the index exists to
remove. It lives on the bridge alongside the occupancy and structure indexes,
keyed on resolution identity, which also makes it impossible to go stale.

### The number

Re-measured with a fresh frame per pick, so the build is not amortised away:

| | µs per item, per pick |
|---|---|
| first cut (D33) | 8.3 |
| D36 — test raw records, construct nothing until a hit | 4.2 |
| **D38 — `ItemGrid`** | **0.64** aimed, **0.01** at interaction range |

```
MAX_ITEMS_PER_CELL = 1000 / (0.64 * 9) = 173      (was 26)
```

Derived by the same rule as before, from the worse of the two cases. **Raising
it further means making a pick cheaper again, not editing the number** — which
is the property that made the ceiling worth having.

### A refactor that nearly shipped a silent bug

Extracting the shared walk, I replaced `SpatialIndex._bucket` with a naive
`int(x // cell)` and lost two things it did: subtracting the cell's `origin`,
and **clamping to the grid** so a point outside the cell falls in the edge
bucket rather than opening a phantom one.

**The full suite stayed green**, because every fixture uses a zero origin and
in-range positions. It was caught by reading the leftover method the edit had
orphaned, not by a test. `bucket_of` now takes both and the behaviour is
restored; the clamp is documented as deliberate, for the same reason
`TerrainCollider.ground_height` clamps.

That is the second time in this project a refactor of working code was safe only
because something outside the test suite noticed. Worth remembering when the
native kernels (D26) arrive.

---

## D39 — "No display" is not "no OpenGL" (corrects D29's probe)

**Caught by the project owner**, on code I had written and tested a few commits
earlier: over SSH, in a container without GPU passthrough, or in a headless CI
environment, my probe would fail to make a context and report *no GL at all*.

### The defect

`gl_renderer_string()` made **one** context attempt with no backend named, and
collapsed every failure - import error, missing driver, missing display, missing
library - into a single string:

> "no OpenGL here - `moderngl` does not import, or no context could be created
> (no driver, no display)"

Two things wrong with that, and the second is worse than the first.

**1. It gave up too early.** `moderngl.create_context(standalone=True,
backend="egl")` needs no window and no `DISPLAY`. A headless Linux box with Mesa
has perfectly good OpenGL through EGL - usually llvmpipe, which is **exactly the
middle tier D29 was written to introduce**. The probe would miss it and fall two
tiers to the pure-Python rasteriser, on the machines most likely to be running
CI.

**2. It misattributed the cause.** "No driver, no display" is a guess covering
four different situations with four different remedies. D29's own justification
was that *silent fallback is how somebody spends an afternoon profiling the
wrong layer* - and this told them to go looking at their display server when the
actual answer was `pip install moderngl`.

### The fix

`gl_probe()` returns `(renderer, detail)` and tries backends in order:

| attempt | why in this order |
|---|---|
| platform default | a workstation finds its GPU in one call |
| `backend="egl"` | no window, no display - SSH, containers, CI |

Three outcomes, kept distinct because they need different answers:

* **No graphics library** - `import moderngl` failed. Install a package.
* **Library, no context** - every backend failed, and the detail names each one
  and its error. Install a driver or Mesa; it is not a display problem.
* **A context** - the renderer string decides the tier, and EGL is *not* a
  synonym for software: a datacentre GPU over SSH reads as hardware.

### Verifying the API instead of inventing it

The first draft of the fix used `create_standalone_context(backend=...)`, which
is not the documented form. Checked before shipping, because this project has
already invented one API that did not exist - `PATCH_MANY` in D34 - and the
lesson from that entry was the point. The correct call is
`moderngl.create_context(standalone=True, backend="egl")`, per ModernGL's own
context and headless documentation.

### What is executable here, and what is not

The build machine has no graphics library, so **only the import branch has ever
run**; the attempt loop stays `no cover` rather than pretended about, exactly as
D22 required of the backend itself.

What *is* exercised is everything downstream, because the probe result is passed
as an argument: `probe_with(renderer, reason)` covers the headless-llvmpipe case,
the headless-hardware case, and both failure causes with their distinct
messages. Six tests, none of which need a GPU.

**That seam is why this was cheap to fix.** D29 split the driver query from the
tier logic specifically so the branches this machine cannot take are still
testable - and the defect turned out to be in the four lines that seam left
uncovered, which is the honest place for a defect to be.

---

## D40 — An accelerator behind the seam, and what measuring it changed

**Assigned by the project owner**: the native kernels are mine to write, not a
library away.

### The toolchain question, answered by looking

D26 deliberately named no toolchain. Checked, this machine has **none**:

```
rustc  absent   cargo  absent   cl  absent
gcc    absent   g++    absent   clang absent   cmake absent
Cython absent   pybind11 absent
```

So Rust + PyO3, C++ + pybind11 and Cython are all unbuildable here, and writing
one would ship code that has never executed a line — the failure mode D22 exists
to name, invoked four times in this log already. I am not doing it.

**NumPy is installed** (2.3.1, SSE/AVX baseline), and it is a compiled C
extension that releases the GIL inside its array loops. It satisfies what D26
actually specified — an optional native path behind a declared seam, absent
without drama — and unlike the alternatives it can be **run, measured and
differentially tested today**. So that is the implementation that exists.

A Rust or C++ kernel remains open. It needs a toolchain installed, not a
decision made.

### D26's third condition, discharged

> **(a) Differential testing, not shared test coverage.** Both paths, same
> inputs, asserted agreement.

D28 built the harness and admitted it was comparing Python with Python. There
are now two genuinely different implementations, and the harness earned its
keep: **2,000 generated cases across five seeds, zero divergences**, with the
committed vectors clean and `lobster conformance --impl numpy` exiting 0.

Everything D28 asserted about *itself* — the mutation tests, the declared
tolerance, the boundary and tie latitudes — was written for this moment, and it
worked first time on the maths. The one bug was mine and structural, not
numerical: `r = p1 - p2` is a column of vectors when there are many capsules, so
the reference's scalar `c` is a column too. The first draft treated it as a
float and numpy refused the shape outright rather than returning a wrong number
— the good kind of failure.

### The measurement changed the design

| kernel | reference | numpy | |
|---|---|---|---|
| `segment_query`, 10 entities | 260 µs | 35 µs | **7.4×** |
| `segment_query`, 1,000 | 3,321 µs | 349 µs | **9.5×** |
| `segment_query`, 5,000 | 16,279 µs | 1,954 µs | **8.3×** |
| `nearest_region`, 6 bones | 46 µs | 107 µs | **0.43×** |

**NumPy loses the refinement by 2.3×**, and that is the finding worth keeping. A
rig has six bones; building six arrays costs more than looping over six
capsules. This is **D26's own seam-granularity argument arriving one level down
than it was aimed**: D26 rejected a per-`dot()` kernel because FFI crossings
would swamp the work, and a per-call kernel over six items fails for the same
reason with a different constant.

So selection is **per kernel, not per implementation** (`KERNEL_PREFERENCE`).
Taking either side wholesale would be slower than the mix. `select()` returns
`"numpy+python"` rather than `"numpy"`, because a log line claiming the fast
path when half of it is the reference would send somebody to the wrong
conclusion — the same discipline as D29's fallback chain.

`nearest_region` would pay off **batched across many targets at once**, which is
precisely the volley shape D26 specified and which is still not built. Until it
is, the reference wins that half on merit rather than by default.

### What is and is not integrated

The kernels sit behind the seam and are proven against it. They are **not yet
wired into `HitTester`**: D26 fixed the seam at *volley* granularity, and
`resolve_projectile` still calls the per-arrow path. Wiring a per-call kernel in
would buy the 9.5× on the broad phase and pay it back in dispatch, which is the
mistake this entry just measured. The volley path is the next piece of work and
it is now the only thing between these kernels and a frame-rate difference.

**L7 still holds.** Nothing outside `lobster/accel/` imports numpy, and a test
walks the tree to keep it that way — the shell drags in no dependency even
though one is installed and used.

---

## D41 — The refinement measured two different rays (found by differential testing)

**Found by writing a second implementation**, which is the entire argument for
D28 arriving as a concrete defect rather than a principle.

`nearest_region` answered two questions about one ray and used a different reach
for each:

* **"Did it strike?"** went through `ray_capsule_hit`, which **normalises**
  the direction internally. Reach = `max_distance` metres.
* **"What was nearest?"** used `far = origin + direction * max_distance`, the
  **raw** direction. Reach = `max_distance × |direction|` metres.

For a unit direction these are the same point and nothing is wrong. For a
direction of length 4, `precise` was judged over 20 metres and `region` over 80.

**Every caller in the tree passes a unit vector**, which is exactly why nothing
caught it: `Selector.pick` normalises, and the conformance generator normalised
every direction it produced. The numpy kernel normalised once and used that
reach for both - the consistent reading - and agreed with the reference on all
2,000 generated cases. It took a hand-built case with a bone beyond the
normalised reach and inside the raw one to separate them.

**Decision: normalise once, in the kernel.** Both questions now measure the same
ray. The reference is authoritative (D28), so the alternative was to replicate
the inconsistency in every future implementation - which would have meant
enshrining a bug as a contract.

No committed vector moved, confirming the change touches only non-unit
directions.

**The generator now emits a non-unit direction in a quarter of cases**, because
a blind spot that hides one bug hides a class of them. That is the second time
this harness has had to grow: D28 recorded the first draft producing 35 empty
results out of 40.

---

## D42 — The native kernel, built and tested twice before anyone runs it

**The project owner's workflow, adopted**: write the source, build locally, run
the differential suite locally, commit the source and not the binary, and let CI
compile it from scratch on three platforms and run the same suite. That is two
independent executions before a player sees it, and it dissolves D22's objection
completely — the rule was never "no native code", it was "no code that has never
executed".

### C, not Rust — and no install was needed

D40 recorded no compiler on this machine. That was true of `PATH` and false of
the disk: **Visual Studio 2019 Build Tools and the Windows 10 SDK are installed**,
and `setuptools` finds them by itself. A trivial extension compiled and imported
on the first try, so the toolchain question answered itself with **zero
installs**.

Given that, plain C against the CPython API over Rust + PyO3:

* **No build dependency at all.** Not cargo, not maturin, not Cython, not
  pybind11. Any machine that can build a CPython extension can build this, and
  the CI step is `python setup.py build_ext` with nothing before it.
* **The surface is tiny and numeric** — a few hundred lines of `double`
  arithmetic over fixed-size vectors, with no allocation beyond the result
  list. That is the case where Rust's safety advantage is smallest and its
  toolchain cost is largest.

Rust remains a reasonable future choice and now needs only `rustup`. Nothing
here is a judgement about the language.

### What it is worth

Measured per call against the reference, and then against a **warm**
`SpatialIndex` because the seam's reference rebuilds one per call and that would
have flattered the result:

| | python (seam) | numpy | **native** | vs warm index |
|---|---|---|---|---|
| `segment_query`, 50 | 361 µs | 47 µs | **2.1 µs** | 107× |
| `segment_query`, 1,000 | 3,351 µs | 350 µs | **42.6 µs** | 11× |
| `segment_query`, 5,000 | 16,658 µs | 1,803 µs | **183 µs** | 7× |
| `nearest_region`, 6 bones | 47 µs | 103 µs | **0.7 µs** | 64× |

**C wins the case NumPy lost.** A six-bone refinement has no array to build, so
the per-call overhead that made NumPy 2.2× *slower* than the reference simply is
not there. `KERNEL_PREFERENCE` puts native first for both, keeps NumPy as the
middle rung for the broad phase where it genuinely wins on a machine with no
compiler, and **excludes it from the refinement** rather than leaving it in to
lose.

### Correctness, before speed

**2,400 differential cases across six seeds, zero divergences**, plus the
committed vectors and the degenerate shapes most likely to take a C kernel down:
empty crowd, empty rig, zero-length ray, zero-length direction, and malformed
payloads that must raise rather than crash.

The C mirrors `lobster/geometry.py` branch for branch, deliberately. The
reference is authoritative (D28), so a change here that is not a change there is
a divergence waiting to be found. Two details that look cosmetic and are not are
called out in the source: candidates sort by `(distance, entity_id)` and not by
distance alone, and `nearest_region` breaks ties toward the **earlier** capsule
because the reference compares with a strict `<`.

Writing it also found D41, which was a real defect in the reference.

### The binary is not committed

`.gitignore` excludes `*.pyd`, `*.so`, `*.dylib` and `build/`. Only
`lobster_accel.c` is in the repo, and every platform compiles its own. A
committed binary would be a private build artefact nobody could reproduce — the
same objection §5 invariant 1 makes about a private save format.

`setup.py` **anchors itself to the repo root**, because `build_ext --inplace`
resolves the package path against the current directory: run from anywhere else
it dropped the binary where the loader would never look, and failed with a
compiler error rather than saying so.

### CI runs both paths — D26 condition (c), discharged

`.github/workflows/ci.yml`:

* a **pure-python job** that asserts NumPy is *absent*, runs the suite, runs
  conformance, and asserts the selected accelerator is `python`. The
  configuration L7 promises is now tested rather than assumed.
* an **accelerated matrix** — Linux, Windows, macOS × 3.11, 3.12 — that compiles
  the C from source, **asserts the kernel actually built** (without that check
  the job would pass by silently falling back, which is the exact failure the
  matrix exists to catch), then runs the suite and both differential
  comparisons.

D26 asked for three things. All three now hold: differential testing against a
real alternative, a declared tolerance with a named authority, and CI running
both paths.

### Still not integrated

The kernels sit behind the seam, proven and fast, and `HitTester` does not call
them. D26 fixed the seam at **volley** granularity and `resolve_projectile` is
still per-arrow; wiring a per-call kernel into it would buy the broad-phase win
and hand back a chunk of it in dispatch. That path is the one remaining piece
between these numbers and a frame rate.

---

## D43 — Two reference bugs the differential suite could not see

Found by re-reading the C before the first CI run that could actually reach it,
not by any test that existed.

### The leak

`segment_query` stashed each surviving candidate's id in a C array, taking a
reference so it would outlive the row it was borrowed from. `Py_BuildValue`
then took **its own** reference for the returned list, and the stashed one was
never given back.

> 2,000 calls, 2,000 leaked references. **2,400 differential cases said
> nothing.**

That is the point worth keeping. D28's harness compares *answers*, and every
answer here was correct. A kernel can be perfectly conformant and still grow
memory until the process dies, and no amount of case generation will find it,
because the property is not about the output at all.

### The dangling borrow

`nearest_region` kept `best_hit` and `best_near` as **borrowed** pointers into a
row it released at the end of each iteration. Safe today only by accident: a
list of lists hands `PySequence_Fast` back the same object, so the inner list
outlives the loop. Hand it a generator of tuples and the winner would be a
pointer to freed memory - a crash or a wrong region, depending on timing, which
is the worst possible shape of bug. Both are owned references now.

A third, smaller: `count` was declared *after* several `goto done` statements
that jump over its initialisation, leaving it indeterminate on the error paths
the cleanup loop reads. Moved to the top with the other declarations.

### What now covers it

Three tests, and one detail that makes them mean anything: **the strings are
built at runtime.** Checking a literal like `"head"` proves nothing, because
interned and immortal strings have a refcount that never moves - a leak and a
clean run look identical. `"".join(("reg", "ion"))` is neither.

The error path gets its own test, because that is where the stash is
half-populated when the loop aborts.

### The wider point

This is the second thing C bought that Python could not have had, and the first
was D41 - a real defect in the reference, found because a second implementation
made a different consistent choice. The costs are visible in the same place: two
memory bugs in three hundred lines, both invisible to a suite that had been
sufficient for everything else.

Neither is an argument against the kernel, which is 79x. Both are an argument
that a native path needs classes of test the pure path never did, and that the
differential harness - however good - is not one of them.

---

## D45 — The volley path: the seam, finally used

D26 fixed the accelerator seam at **volley** granularity and argued the case in
the abstract. D40 and D42 built kernels behind it and measured them at 9.6x and
79x. Nothing called them, and every entry since has had to say so. This is that
gap closed.

`HitTester.resolve_volley(view, shots)` takes
`(origin, direction, max_distance, force, source_id)` tuples and returns one
list of hits per shot.

### The property that had to hold first

> **`resolve_volley(shots)` answers exactly what N `resolve_projectile`s
> answer** - same regions, same order, same `region_precise`, same snapshot
> provenance, and the same Events in the same order.

Asserted directly across three world sizes and both `first_hit_only` settings.
A faster path that answers differently is not a faster path, and none of the
timings below would matter if that failed.

### What a volley shares that separate calls cannot

1. **The entity table is packed once.** Turning the spatial index into a kernel
   payload is O(entities); per arrow it would be O(arrows x entities) - the
   population scaling Scope §7 exists to avoid, reintroduced by the
   optimisation meant to remove it.
2. **Each rig's hitboxes are resolved once**, so `limb_state` is read once per
   entity per volley rather than once per entity per arrow. §13 permits exactly
   this - *"never cached beyond the current hit-test or frame"* - and a volley
   is one frame's hit-testing. A limb severed *by* this volley is not visible
   to it, which is already true per-arrow: `resolve_projectile` reports, it
   does not apply damage (L4).
3. **The budget is charged once**, against the accumulated counters. One
   frame's work, one decision about whether that frame fits.

### What it bought

| | per-arrow | volley | |
|---|---|---|---|
| 200 entities, 200 shots | 70.0 ms | **10.7 ms** | 6.6x |
| 500 entities, 200 shots | 80.2 ms | **16.3 ms** | 4.9x |
| 1,000 entities, 200 shots | 89.2 ms | **19.1 ms** | 4.7x |

Against the 2 ms frame slice (D27), that is **6 arrows per cell per frame
becoming 21-38**, depending on crowd density.

### Where the time goes now, which is the useful part

Profiled rather than guessed, twice - the first guess was wrong.

Caching the *packed* capsule lists as well as the capsules made no measurable
difference, so marshalling was not the bottleneck. The profile named the real
one:

| | share of a 200-shot volley |
|---|---|
| `Skeleton.hitboxes` (six capsules per rig, quaternion maths in Python) | **~50%** |
| `segment_query` - the C broad-phase kernel | **15%** |
| everything else | ~35% |

**The broad phase is no longer the problem.** That is the seam working: the
thing D26 measured at 64% of a hit-test is now 15% of one, and the cost has
moved to posing rigs.

### What this does not do, and the next kernel it names

`Skeleton.hitboxes` is now the dominant cost and it is **not behind the seam**.
Accelerating it means a third kernel - pose to capsules - which D26 did not
scope and which is a larger surface than the two it did: it would take a rig's
bone hierarchy rather than a flat array. Logged rather than built, with the
profile above as the argument for whoever picks it up.

Also not built: a **cross-cell** volley. `WorldHitTester.resolve_projectile`
handles one shot across the resident set (D23); batching that means one payload
per cell and merging by world distance. The single-cell volley is where the
measured win is, and the cross-cell path is unchanged and still correct.

### A test-harness mistake, for the second time

A fixture helper called `tester` was collected and run as a test, because
`unittest` takes any method beginning with "test". The same thing happened
earlier in this project and was fixed the same way. It is now named
`make_tester` with a comment saying why, which is the only defence available
short of not using the word.

---

## D46 — The sink is symmetric about residency (Shrimp finding #2)

**Their report, and it was right.** `OctopusEventSink` refused to forward
`on_enter_cell` for exactly the correct reason and forwarded `on_exit_cell`
anyway.

> any consumer that binds a Trigger to `on_exit_cell` fires it on ring churn -
> a cell two hops away being released - including for cells the player was
> never in. `set_player_cell` load-then-unload can fire several per transition.

The asymmetry is indefensible on its own terms. CONTRACT §1 has said since it
was written that *"loading a neighbouring cell for residency is not the player
entering a scene"*; releasing one two hops away is not the player leaving, by
precisely the same argument. Half of that reasoning was implemented.

### Decision: both of their options, minus the one the contract forbids

They offered three remedies. Two are taken:

1. **`on_exit_cell` is no longer forwarded**, so the two residency Events are
   handled identically.
2. **`sink.exit_scene(location_id)` exists**, the explicit counterpart to
   `enter_scene`. It fires `on_exit_scene` as an open-string trigger, because
   Octopus exposes `enter_scene` and has no `exit_scene` of its own - which is
   exactly the workaround Shrimp was maintaining, now owned by the layer that
   should own it.

The third - *"rename the residency signal so the two cannot be confused"* -
**cannot be done.** §13 fixes the seven Event names and `CONTRACT_EVENTS` is
asserted against that list exactly; renaming one would break every consumer
bound to it to fix a naming problem. What was available instead is removing the
*consequence* of the confusion, which is what forwarding it caused, and saying
so plainly in CONTRACT §1.

### Why it survived being documented correctly

**No test asserted what the sink forwards, in either direction.** The rule was
written down accurately in CONTRACT §1 and implemented for one of the two
events, and nothing compared the two. That is the same shape as D24 - a
document describing behaviour nobody checked - and the fix is the same: pin it.

Four tests now do, including one that reproduces the reported symptom by
walking a player between cells and asserting that the resulting churn reaches
Octopus as nothing at all. Verified by mutation, and the first mutation attempt
was **too small to fail** - reverting only the early return left the event with
no bindings branch, so it still was not forwarded. Reverting both halves fails
three tests.

### What this does not change

The Events themselves are untouched: `on_exit_cell` still fires from
`CellManager.unload`, still means residency, and is still what `GpuResidency`
uses to release buffers (D44). It was never the Event that was wrong - only its
onward journey into a trigger vocabulary where it reads as something else.

---

## D47 — The model library: what a model *is*, by the time anything draws it

ASSET_SCOPE §7 step 2 asked for "the library artifact, both kinds" and named the
format:

> Mesh each distinct model once — voxels through the existing `StructureMesher`,
> primitives through a dozen-line generator — **into the same vertex format**;
> write `models.lobster_lib`; a reader with the same provenance discipline as
> `read_bundle`. **Primitives first**, because they need no file and so prove the
> library end to end before the importer is involved.

That is what got built. `models.lobster_lib` holds each `Model` once as a packed
triangle stream — `position, normal, tint`, nine floats a vertex — and by the
time geometry is in it, nothing downstream can tell a `.vox` from a box.

Building it forced five questions the scope did not answer. Each is recorded
here because each is the kind that gets decided by whichever line of code runs
first.

### 1. Where is a model's origin?

**Ambiguity.** A `PropPlacement` carries a `Transform`. Geometry out of the
mesher spans `0..side` on every axis, because that is where the voxel grid is. A
`.vox` file has no anchor convention at all.

**Chosen: centred on X and Z, sitting on `y = 0`**, taken from the *meshed*
extent.

The decisive argument is **rotation, not tidiness.** A placement transform
rotates about the model origin. A corner-anchored crate turned 45° does not turn
in place — it swings around its corner and ends up somewhere else. "Rotate this"
and "move this" would be the same field, and the bug would look like a content
error every time.

Taken from the meshed extent and not the grid, because `to_structure` pads a
`.vox` up to a cube whose side divides by `MICRO_CHUNK_VOXELS`. The padding is
empty and produces no faces, so anchoring on the grid would offset every model
by half its padding — silently, and only the small ones. Mutation-tested with a
2-voxel model in an 8-voxel cube: anchoring on the grid puts it 0.75 m away.

Resolution order note: this is criterion 3, *deterministic attributable
failure*, over criterion 5. The zero-policy answer was "store it as meshed and
let the author compensate", which is cheaper here and wrong everywhere a model
is ever rotated.

### 2. Which palette?

**Ambiguity.** A `.vox` carries its own 256-entry `RGBA` table. Lobster already
has `DEFAULT_PALETTE`, which is what structures are drawn with. Two palettes,
one `material` field.

**Chosen: resolve to RGB at build time, per kind.** A voxel model uses its own
file's table (index *i* is entry *i − 1*); a primitive, and a `.vox` with no
`RGBA` chunk, uses `DEFAULT_PALETTE`.

This looks like two rules and is one: **a model in the library holds colour,
never an index**, so no runtime code ever has to ask *which* palette a model
meant. The question only exists at build time, which is where it is answered and
then destroyed. ASSET_SCOPE §1's whole case for `.vox` was that "a `.vox`
carries a 256-entry palette, so the *or* is already answered" — ignoring that
table would have thrown away the thing that argument rested on.

### 3. Lighting

**Chosen: library models are unlit.**

A cell bundle multiplies its baked lightmap into its vertex tints, which is free
and correct precisely because a cell's light cannot change during a residency
(§7). A *shared* model has no cell — that is what makes it shareable — so baking
any cell's light into it would be baking one cell's light into every cell.

This is a bill deferred, not avoided: step 5's instanced draw needs a
per-instance light term, and that is where it will be paid.

### 4. Which models get meshed?

**Chosen: every declared `Model`, not only the referenced ones.**

Meshing on demand would make the artifact depend on which cells happen to place
what, so adding a prop to one cell could change another cell's build. The
linter already reports art nobody names (`model_asset_unused`, still a
warning), and an unplaced model costs bytes in a derived file that is safe to
delete.

### 5. How many sides has a cylinder?

**Chosen: `CYLINDER_SEGMENTS = 12`, a constant and not a record field.**

Same argument as freezing the shape set. A knob whose only effect is triangle
count is a knob content gets wrong, and freezing it makes a cylinder's cost a
number the budget (§6) can multiply rather than one it has to look up per model.

### Three new lint codes, and where they fire from

| code | fires from | why it is an error |
|---|---|---|
| `invalid_primitive_dimensions` | `lint.check_models` | a zero, negative, missing or non-numeric size meshes to nothing |
| `model_meshing_failed` | `library_writer.build_library` | the `.vox` could not be read; the record id is attached to the reader's own message |
| `model_meshes_to_nothing` | `library_writer.build_library` | at runtime, indistinguishable from a missing model |

The last two are emitted by the *build*, not by a check, for the reason
`over_budget` is: they are only knowable once something has been meshed. So the
library is built **before any cell**, and a world with a broken model bakes
nothing at all rather than bakes partly — the same rule §15.2 already applies to
a one-way connection. Mutation-tested by moving the library build after the cell
loop: a cell bundle appears on disk beside a failed build.

The dimension rule has exactly one definition. `lint` calls
`model_mesher.primitive_dimension_problem`, the same function the generator
calls before it meshes, so a rule cannot hold in one and not the other. Mutating
the lint to invent its own answer fails.

### A guard that could not fail, found by mutation

The reader checks the header's `vertex_count` against the blob's length, and
separately that the count is whole triangles. Written in that order, the second
check was **unreachable**: any count that is not a multiple of three also fails
the length comparison, so the test written for it passed with the check deleted.

The order is now reversed and both are reachable and independently mutable. This
is the D30 lesson again in a smaller frame — a guard nothing can make fire is a
comment with a syntax error budget — and it was caught only because every new
guard in this project gets mutated. Thirteen mutants, thirteen caught.

---

## D48 — Counting references to a shared model, and whose lifetime an item is

ASSET_SCOPE §2 chose per-residency model loading over loading the whole library,
and said why:

> **Recommend the second**, because it is the one that keeps L6 meaningful, and
> because the machinery exists: `GpuResidency` already uploads and releases per
> cell and already has a `drift()` check for exactly the kind of leak a
> reference count invites.

It then listed five invariants. All five are implemented and each has a test
named after it. What the scope did not settle is below.

### 1. A reference is per distinct model per cell, not per placement

Fifty barrels in one cell hold **one** reference.

Both schemes balance arithmetically, so this is not a correctness question - it
is a question of what the number *means*. Per-placement, the count is "how many
barrels are standing in resident cells", which is a fact about decoration and
changes when an author adds one. Per-cell-per-model, it is "how many resident
cells would miss this buffer if it went", which is exactly the question
`release_model` is asking.

The tiebreak is invariant 5, the one that catches leaks: it is about the *set*
of live models, not the total, and a set is what a per-cell count produces
directly.

### 2. Who reads `models.lobster_lib`

**`CellManager`, not the renderer.** It is a geometry build artifact living in
the bundle directory, and that is the class that owns that directory. Handing
the renderer a path would put geometry back inside presentation - the L8 mistake
RENDER_SCOPE §4 avoided by making the backend a *subscriber* to residency rather
than a caller into it.

**A world with no library loads.** Every world built before step 2 has none, and
refusing to load a cell over a missing derived artifact would break them all for
a file that is safe to delete by definition (D1). What is not silent is the
consequence: `missing_models` counts it and `unresolved_models()` names the
model *and the cells that asked*, because "a model is missing" answers nobody.

A library that exists and is *corrupt* is the opposite case and raises. An absent
derived artifact is a world that predates a feature; a corrupt one is a build
that lied.

### 3. Items: whose lifetime is a placed item's model?

**Deferred to step 5, deliberately, and this is the entry that says so rather
than leaving a hole.**

A prop is baked into the bundle, so its model is fixed for the cell's whole
residency - which is what makes a per-cell reference count correct. A placed
`Item` also carries a `model_ref` (§8, D33), and an item can be picked up
mid-residency, so its model's lifetime is the *item's* and not the cell's.

Three reasons this is the right place to stop, and not one of them is "it was
harder":

1. **The software path needs no residency at all** (step 4). It reads the
   library directly, so items get real geometry there without any of this.
2. **`DrawItem` does not carry `model_ref` yet.** It gains one in step 4, which
   is where the question stops being hypothetical.
3. **The alternative available today is worse.** `on_item_placed` carries
   `item_id`, `cell_id` and `transform` - not `model_ref` - so counting item
   models now would mean adding a field to a payload CONTRACT §13 freezes, to
   serve a draw path that does not exist. D46 already established that the Event
   surface is not the place to absorb a downstream convenience.

Until then `ResidentCell.model_refs()` documents itself as props-only in the
docstring rather than in a comment nobody reads, and CONTRACT §7 says the same.

### 4. The third link nothing was checking

Octopus's `dangling_reference` covers `Item.model_ref`, because that is a record
field in its schema (`lce/lint.py`'s `_REF_FIELDS`). A `PropPlacement` lives in
the **manifest**, so its `model_ref` had no owner at all, and a typo produced a
barrel that was baked into a cell and then drew nothing.

`prop_model_ref_unresolved` closes it, and an **empty** `model_ref` is
deliberately not a fault: a prop with none is the impostor CONTRACT §10 has
always described, and turning a documented absence into an error would fail
every world built so far.

### A leak check that could not report a leak

Mutating `drift()` to compute `needed` as `set(self.model_counts)` - comparing
the counts against themselves - made the model half always agree and **passed
every test there was.** Two tests now inject a leak and a gap directly and
assert `drift()` reports each.

Same shape as the reachability bug D47 records one entry earlier, and the same
lesson: the tests were asserting that the *world* was consistent, which it was,
rather than that the *check* could tell when it was not. Sixteen mutants,
sixteen caught.

---

## D49 — Drawing a model, and the pixel test that could not see a wrong one

ASSET_SCOPE §7 put the software path before the GPU path and said the step was
not skippable:

> It is the only place a wrong mesh is **visibly wrong** rather than merely
> *different from the GPU*. RENDER_SCOPE §2 deleted pixel agreement between the
> backends, which was right and which also means the GPU path has no oracle: a
> mesh drawn inside-out, at the wrong scale, or with inverted normals would
> render, differ from the software path, and be indistinguishable from the
> legitimate differences that decision permits.

Props and items now draw as their library geometry. Four choices, then the part
worth reading.

### 1. How the library reaches the renderer

**A keyword argument on `render`, defaulted to `None`** - not a constructor
field, and not a lookup the renderer does itself.

The two backends need it for opposite reasons, which is the argument for a
parameter rather than state: a CPU rasteriser walks the meshes every frame and
cannot draw a model without one; a GPU backend already holds the buffers
residency uploaded (D48) and needs only the `model_ref` on each `DrawItem`. A
constructor field would make the software backend stateful about something that
belongs to a *world*, and a backend that read the file itself would put geometry
back inside presentation - the L8 mistake RENDER_SCOPE §4 already avoided once.

`render_resident` reads it off the manager, which owns the bundle directory.
Nothing in the render path opens a file.

### 2. `DrawItem` carries a transform, not only a centre

A model is placed by two transforms in order - the object inside its cell, then
the cell in the world - which is exactly the two steps `_draw_structure` already
does for a structure origin.

The draw list previously carried only a world `center`, which was enough for an
impostor because an impostor is a camera-facing quad and has no orientation. A
centre cannot turn a crate 45°, and D47 chose the model anchor specifically so
that rotation would mean *turn in place*. Carrying the transform is what makes
that decision reach the screen.

### 3. The cull radius now comes from the model

`max(invented_guess, mesh.bound_radius())`.

The guesses below it - `1.0` for a prop, `ITEM_DRAW_RADIUS_M` for an item - were
invented precisely because nothing knew a prop's extent. A model does. This is a
cull and not trivia: a 3 m statue culled against a 1 m guess pops out of view
while it is still on screen.

**Never smaller than the guess**, because `visibility.py` already states the
rule - *a cull must err towards drawing* - and a tight bound on a shape whose
projected extent this file does not model is the wrong direction to be wrong in.
`bound_radius()` measures from the model **origin** and not from the centre of
its bounds, so the number does not change when the thing is turned.

### 4. Lighting, and the impostor that is staying

The library tint is unlit by construction (D47), so the cell's baked light is
multiplied in at draw time, at the triangle centroid, through the same
`_light_at` every other surface samples. A second sampler would be a second
thing to keep correct.

The impostor stays. It is what a thing with no mesh looks like (§4), and there
are three honest ways to get one: no library, an empty `model_ref`, or a ref the
library does not hold. The third is a build error and a counted residency fault,
so it is named in both places rather than raised mid-frame - a renderer that
threw over one absent barrel would take the whole picture with it.

### The part worth reading: a pixel test that could not see a wrong picture

Three mutants survived the first pass, and two of them survived for the same
reason - **the test measured the world rather than the check.**

**"Which pixels did this prop draw?"** was answered by *"the ones that are
neither sky nor ground"*, with the ground taken to be the commonest non-sky
colour. That is a reasonable heuristic and it is wrong in exactly one case: when
the terrain moves with the cell. Dropping the cell placement from the model
drawer left the crate stationary while the ground slid under it, the heuristic
followed the ground, and the assertion about *where the crate went* passed.

It is now a difference against the same scene rendered with **no props at all**,
which is the literal definition of the question. Each shot is compared against
its own baseline, so terrain moving with the cell cannot stand in for the crate
moving with it.

**"Do the three shapes draw different pictures?"** could not see a tint bug: a
crate and a barrel both use material 6, so replacing every tint with white left
them still telling each other apart by geometry. Two boxes of the same size and
different materials now assert the colours differ, that neither is white, and
that the hues follow the palette.

The third survivor was a bad mutant rather than a gap - `return mesh` where
`mesh` is already `None` changes nothing - but writing the honest version of it
found a real hole: a library holding a model with **zero triangles** was never
tested, and that is a bundle and a library built apart rather than a hypothetical.

A fourth survivor turned up after those were fixed, and it is the same lesson a
third time: `render_resident` hands the library to **two** consumers - the culler
and the backend - and dropping the first leaves every model still drawn, with
only the cull radii reverting to guesses. No frame comparison can see that. A
test now reads the draw list the backend was handed.

Sixteen mutants across two passes; four survived; all four are caught now. And
the pictures were looked at, which is how the cylinder cap and the turned crate
were confirmed rather than assumed.

### One more, about the harness rather than the code

The mutation runner restored the source in a `finally`, which covers the script
failing and not the script being **killed**. One run sat long enough to be
stopped from outside, the in-flight mutant stayed in `raster.py`, and the next
test run failed for a reason that had nothing to do with the code under test.

Two fixes, both worth keeping: the runner now writes the original to a sentinel
file *before* editing and restores whatever the sentinel names at startup; and
the frame comparisons no longer go through `assertEqual` on hundred-thousand-byte
lists, because unittest's differ is superlinear and builds that diff precisely
when a mutant is caught. That is why the run was slow enough to want killing.

---

## D50 — Instancing, and the item question D48 deferred

ASSET_SCOPE §7 step 5:

> **Drawing, GPU, with instancing.** Draw-call assertions against
> `RecordingContext` first: one upload per model, one instanced draw per model
> per cell, no per-placement buffer.

All three hold, and one of them holds more strongly than asked.

### 1. One draw per model, not per model per cell

The scope said *per model per cell*. The instance matrix is the **world**
placement — the object inside its cell composed with the cell in the world — so
the cell stopped being a grouping key: fifty barrels spread across three
resident cells are one draw, not three.

Strictly fewer draws, and it protects exactly the property the scope was after
(no per-placement buffer, no draw per barrel). Recorded here rather than
silently improved on, because a scope line and the code disagreeing is the thing
this log exists for.

### 2. A second program, not a branch in the first

The static path keeps its `model` uniform. Converting terrain and structures to
one-instance draws for symmetry would be rewriting working, tested code for
elegance — the trade L8 refuses. The fragment stage is shared, so the two
programs differ only in where a placement comes from.

The cost is one extra `mvp` write per frame, which is why the uniform-write
budget test now says `2 + cells + 1` and says why. And a mutation that dropped
the second program's `mvp` **survived the first pass**: no draw-call count can
see it, because the draws still happen — they just collapse to the origin.

### 3. The light is per instance, which is the bill D47 deferred

A library tint is unlit by construction: a mesh shared by every cell that places
it cannot carry one cell's bake. So each instance carries 16 matrix floats plus
one float of baked light, sampled at the placement. That is what §3 already asks
of structures — *"structures sample ambient at their position"* — and it is one
sample per prop per frame rather than one per vertex.

### 4. Items are counted after all

D48 deferred this with three reasons and named step 5 as the place. Here is the
answer, and the reason it turned out cheap:

**`CellManager` can read the resolution without opening a view.** That was the
obstacle. `OctopusBridge.frame()` closes any view already open, so opening one
from inside a residency handler would pull the caller's frame out from under it
mid-render. Reading `session.resolution()` directly does not, so
`item_model_refs(cell_id)` is callable at any moment — which is what made the
whole thing possible without adding a field to a payload CONTRACT §13 freezes.

So `GpuResidency` subscribes to `on_item_placed` and `on_item_removed` too, and
`_sync_cell` — one primitive — serves all three: entering syncs from nothing,
an item event syncs a difference, leaving releases everything. The ordering is
free: `ItemPlacer`'s first docstring line is *"Writes item placement, then
reports it. In that order"*, so the resolution is already current when the Event
arrives.

A manager with no session answers no item models. It is the same manager that
cannot place an item either, so there is no case where this loses something a
caller could otherwise have had.

### Three defects found, none of them in the new feature

**A leak in `release()`.** It freed the framebuffer, both textures and the
static program — and not the instanced one, which this entry added. Found by the
first assertion that looked at `RecordingContext.live_resources()` after
teardown, which is a check that existed and had never been pointed at this
method.

**A class attribute pretending to be instance state.** `_missing` — the set of
cells a draw list named that were never uploaded — was declared at class level,
so every `ModernGLBackend` in a process shared one, and a test could inherit
another test's complaint. Pre-existing; found by the same assertion.

**A guard that could not fail, again.** `release()` had a second loop freeing
instance buffers after `release_model` had already taken them. An instance entry
cannot exist without its model — `_instance_target` needs the model's vertex
buffer to build the VAO — so the loop was unreachable, and it survived mutation
for the only reason unreachable code ever does. Deleted, with the invariant
written where the loop was. Third time this pattern has appeared in three
entries (D47, D49, here), which is either a habit worth naming or a sign that
mutation testing is doing its job. Probably both.

Eighteen mutants, eighteen caught.

---

## D51 — The budget, taken last, and the three things it made me fix first

ASSET_SCOPE §6 put this step at the end and said why:

> **No placeholder ceiling. Not even a temporary one.** ... A number invented
> before there is anything to measure survives: it acquires tests, it gets
> quoted in a document, and by the time real numbers exist it is load-bearing
> and nobody remembers it was a guess.

So it was taken last, and the first measurement said the feature was unusable.
That is the whole value of the ordering.

### The measurement, and what it cost to make it honest

The unit ASSET_SCOPE named was *"meshed bytes per placement"*. That unit stopped
existing at step 2: a shared library means mesh bytes are per **model**, and
what a placement costs is a *frame*, not memory. So there are two budgets, and
the scope's own sentence is the casualty of its own step 2. Worth saying plainly
rather than quietly measuring something else.

The per-placement frame cost, measured on the slope from 25 to 800 placements in
one cell, taking the worst slope rather than the median (D38's choice, same
reason):

| | us/placement | what changed |
|---|---|---|
| first cut | **29.9** | two 4x4 matrices and a 64-multiply product per placement; a bound radius recomputed from eight square roots every frame |
| compose | **15.3** | rigid transforms composed as a quaternion product; `ModelMesh` computes its radius once |
| camera | **12.8** | `Camera.basis()` and `tan_half_fov()` computed at birth instead of inside every cull test |

At 29.9 the derivation lands on **eight props per cell**, which is a correct
division and a useless ceiling. The project's own answer to that is on the page
in D38 — *"raising it further means making a pick cheaper again, not editing the
number"* — so three things got fixed before any number was written down:

1. **`Transform.compose`.** Every placement is rigid, which the shader comment
   already relied on, so composing two of them is a quaternion product and one
   rotated vector rather than two matrix builds and a 64-multiply product.
2. **`ModelMesh.bound_radius`** was eight square roots, recomputed per placement
   per frame, for a pure function of immutable bounds. Computed once now.
3. **`Camera.basis()` and `Camera.tan_half_fov()`** were recomputed *inside*
   every `sees_sphere`. A 400-prop cell orthonormalised the same basis 3,600
   times a frame and called `math.tan` twice as often. Both are pure functions
   of immutable fields; both are computed at construction now.

The third was not a model bug at all. It was in the culler every backend has
used since the beginning, and it took profiling a budget to notice - which is an
argument for deriving budgets rather than choosing them that I had not expected
to be making.

### The numbers

**Memory.** The library is the first resident thing that is not per cell, so it
cannot be charged to one - a barrel two cells both place would be paid for
twice. It gets a share of the transition peak on the same terms as a cell, and
D20's divisor gains one: `RESIDENT_MEMORY_SHARES = MAX_RESIDENT_CELLS + 1`.
`DEFAULT_MAX_CELL_BYTES` drops from 21.3 MiB to 19.2, which is the honest price
of a tenth claimant and the same argument D20 made in the first place - a
default that cannot compose is a lie.

Checked against the **whole** library at build time rather than the resident
subset at runtime: conservative, exact, and one number instead of a second
ledger. If the whole thing fits, every subset does.

**Frame.** `MODEL_DRAW_BUDGET_US = 4000` is a declared slice, bigger than
hit-testing's 2000 and picking's 1000 because it is what a frame is *for*, and
small enough that the three together are 7 ms of a 16.7 ms frame - under half,
with the majority left to the game above. Divided by the measured 12.8 that is
**312 visible placements a frame**, and divided again by the ring, **34 props a
cell**.

### Two ceilings that look like a contradiction and are not

> **Wrong, and superseded by D52.** This section defended a per-cell ceiling
> that counted props while items fed the same frame counter, and it defended a
> ring divisor applied to interiors that have no ring. The paragraph below is
> kept because the *argument* it makes about picking is still true and is the
> reason `MAX_ITEMS_PER_CELL` survived unchanged - but "that is the two ceilings
> composing, not colliding" was not true, and two questions from the project
> owner took about a minute to show it.

`MAX_ITEMS_PER_CELL` is 173 and `MAX_PROP_PLACEMENTS_PER_CELL` is 34. Both are
derived; neither is wrong.

A pick tests every item in **every** resident cell with no culling at all, so
173 is a fixed worst case. Drawing culls, so its worst case is not "every cell
full" but "this many things on screen" - which is why the drawing ceiling is a
frame counter and the picking one is a per-cell count. A cell that fills its
item allowance *and* puts every one of them on screen trips `model_frame_us`,
loudly, naming the metric. That is the two ceilings composing, not colliding,
and there is a test that says so in one line.

### Octopus's own hook

§6 asked that `cost_estimate` be *"honoured rather than re-derived"*. Honouring
it here means reporting the truth against it: Lobster measures the mesh exactly
and cannot write the record back, so a declared estimate the geometry has
outgrown becomes `model_cost_estimate_low` - a **warning**, because an out of
date estimate is dead weight in a report rather than a broken build. Zero means
"not declared" (it is Octopus's default for the field) and is never a finding.

### And a mutation harness that was judging nothing

Four of this entry's mutants were reported SURVIVED without ever running:
`python -m unittest tests` runs **zero** tests and exits 0. The dangerous
direction is worse - a mistyped target errors, exits non-zero, and is reported
*caught*, which is a false pass.

The harness now runs every target once unmutated and refuses to believe a
verdict from one that does not both pass and report a non-zero test count. Two
of the four mutants it had been ignoring were real gaps: nothing tested
`Transform.compose` against applying both transforms in order, and nothing built
a world with too many props.

Twenty mutants, twenty caught - and this time the count means something.

---

## D52 — Two worst cases applied to populations that did not have them

D51 shipped a per-cell drawing ceiling of 34 and a paragraph explaining why that
did not contradict `MAX_ITEMS_PER_CELL`. The project owner asked two questions,
a minute apart, and each one found a defect the paragraph was covering for.

> Isn't one items and one NPCs?

Neither. `MAX_ITEMS_PER_CELL` is placed `Item` records (D38, derived from
picking); `MAX_PROP_PLACEMENTS_PER_CELL` was bundle props (D51, derived from
drawing); NPCs are `DEFAULT_MAX_ACTIVE_SKELETONS_PER_CELL`, which is 32 and a
coincidence. But the question was the right one to ask, because the two numbers
I *was* comparing are two different populations - and **both of them draw**.

### Defect 1: a ceiling on one contributor, derived as though it bounded both

`charge_placement` fires for props and for items. The per-cell ceiling counted
props and divided the frame budget by the ring anyway. So:

* a cell at 34 props **and** 173 items is 207 drawable things;
* two such cells is 414 against a frame ceiling of 312;
* and every per-cell check passes.

That is precisely the failure D20 named - *a default that cannot compose is a
lie* - which I quoted in the commit message while shipping it. The paragraph
defending the two numbers as "different questions" was true about *picking* and
irrelevant to the thing that was broken.

**Fixed:** one ceiling, `max_drawable_placements`, over props **and** items with
a `model_ref`. Counted at build time (the build can see both - props are in the
manifest, items are records with a resolvable location) and at `place_item`,
which already refuses a cell at its `max_items` and now refuses one at its
drawing ceiling for the same reason and in the same place: before the write, so
a refusal leaves nothing behind.

`MAX_ITEMS_PER_CELL` is untouched and keeps its meaning. It bounds *items*,
mesh or no mesh, because a pick tests all of them. A cell may still hold 173;
what it may not do is give them all models.

### Defect 2: an interior charged for a ring it can never have

> That doesn't seem very high. If someone has a 'player home', then 34 items is
> quite small.

Correct, and the reason is not the slice or the unit. It is that 34 is
`frame ÷ MAX_RESIDENT_CELLS`, and `residency_ring` says in its own docstring:

> An exterior cell brings its exterior neighbours; **an interior brings
> nothing** (DECISIONS.md D8).

A player home is an interior. It is resident *alone*. Dividing the frame budget
by nine charges it for eight exterior neighbours that will never be loaded
beside it, and a house with 34 things in it is not a house.

**Fixed:** the ceiling is derived from the cell's actual residency worst case -
`MAX_DRAWABLE_PLACEMENTS_PER_CELL` (34) for an exterior, and
`MAX_DRAWABLE_PLACEMENTS_PER_INTERIOR` (312, the whole frame) for an interior.
This is the only per-cell default in the shell that is not global, and it is not
global because residency is not. `Budget.declared` reads the `exterior` tag off
the Location record, which is the same test `cell.is_exterior` uses - one
definition, not two.

A transition into an interior does hold both sets briefly, but `set_player_cell`
loads and unloads inside one call, so no frame is drawn with both resident. The
overlap is a *memory* peak, which `MAX_TRANSITION_PEAK_BYTES` already bounds.

### What is actually left, if 312 is still not enough

312 is the pure-Python ceiling at 12.8 us a placement, and that unit is the only
thing standing between it and a larger number. Three quarters of it is culling
and transform work that is uniform across placements - the shape the accelerator
seam (D26) exists for, and the fourth line of D51's table. Raising the ceiling
means writing that kernel, not editing the constant.

### The shape of both defects, which is the same shape

Each took a worst case that was true of one population and applied it to another:
props' frame cost applied as though items had none, and the exterior ring
applied to cells that have no ring. Both survived because every test asserted
the *arithmetic* of the derivation - which was correct - rather than the
*property* the derivation was supposed to guarantee.

There is now a test for the property itself: nine exterior cells at their
ceiling, everything on screen, still inside the frame budget. It fails under
either defect. Thirteen mutants, thirteen caught.

---

## D53 — The third kernel, and what was actually left after it

The project owner asked for it, having been told the lever on the placement
ceiling was the 12.8 us unit rather than the constant. Two things in this
conversation had been called "the third kernel" - the pose-to-capsule one on the
backlog (D45) and this one - and the one that was asked for is the one the
sentence before the question named. Pose-to-capsules stays on the backlog.

### The seam, and why it goes where it does

`place_batch(payload)` culls one cell's placements against the camera and packs
the instance data for the survivors, **in the same call**.

One call per resident cell per model per frame. That granularity is D26's
argument for the third time: the intermediate between culling and packing - a
world transform, a frustum verdict, a 4x4 - never crosses the boundary, because
crossing it per placement *is* the cost. And returning both answers together is
not a convenience: the culler has the world transform and the sampled light in
registers at the moment it decides the thing is visible, so handing them back
would mean computing them twice.

**The input rows are built once per residency, not per frame.** A prop does not
move. Rebuilding a flat array of them every frame would be per-placement Python
work in front of a kernel whose whole purpose is removing per-placement Python
work - the same trap one level up, and the same answer `upload_cell` gives.

Items are the exception and are rebuilt each frame: they are records that can be
dropped or picked up mid-residency, so caching them needs an invalidation story.
Reading seven floats out of a dict is a fraction of what the kernel removes, so
it is still most of the win. Measure before caching that.

### Harness first, again

`lobster/conformance.py` gained the kernel, the reference, 34 generated cases
and a comparator **before** a line of C was written - the order D26/D28 fixed,
and for the reason that entry gives: a native implementer gets a target to hit
rather than a description to interpret.

The reference calls the real `Camera.sees_sphere`, the real `Transform.compose`
and the real `geometry.matrix4`/`pack_matrix4`. Getting the last of those meant
**moving them out of `gl_backend` into `geometry`**, where a `Transform` as a
4x4 belongs anyway - the alternative was a reference that kept its own copy of
the maths it is the reference for, which is exactly the drift
`reference_segment_query` avoids by calling `SpatialIndex.query_segment` itself.

Two declarations the existing kernels did not need:

* **`UNIT_TOLERANCE`**, separate from `DISTANCE_TOLERANCE_M`. Comparing a
  direction cosine against a metre tolerance is a category error that happens
  to work while the numbers are near 1.
* **A bytes-aware vector encoding.** The payload carries a lightmap and the
  result carries packed instances; both are `bytes` at runtime, which is the
  point, since hex on the hot path would cost more than the kernel saves. So
  the *file format* tags them and the runtime never sees it.

### The measurement that set the preference, and the one that overturned it

| batch | python | numpy | native |
|---|---|---|---|
| 10 | 7.9 us/pl | 32.0 (0.2x) | 0.43 (18x) |
| 800 | 7.4 us/pl | 0.90 (**8.2x**) | 0.15 (**50x**) |

On that table numpy looks like an obvious middle rung. It is not, and seeing why
took measuring the **aggregate rather than the single call**:

| a real frame | python | numpy | native |
|---|---|---|---|
| exterior ring: 9 cells x 4 models x 9 props | 2.6 ms | **10.0 ms** | 0.10 ms |
| one interior: 6 models x 52 items | 2.4 ms | 1.8 ms | 0.05 ms |

The kernel is called once per cell per model, so a real frame is dozens of small
batches - and across an exterior ring numpy is **four times worse than not
accelerating at all.** Not marginally: it would have taken a frame that fitted
and broken it.

That is D40's finding a third time. The granularity that makes C fast is exactly
the granularity that makes numpy slow, and a benchmark of one big call would
have shipped the wrong preference with a table to back it up. `place_batch` gets
`("native", "python")`; numpy still *implements* it, so the differential suite
holds it to the same answers and a caller may ask for it by name.

### What the wiring found

**A module import per draw item.** `_draw_item` opened with
`from ..visibility import ENTITY, ITEM, PROP, STRUCTURE, TERRAIN` - 2,020 trips
through `importlib` for a 400-prop frame. Pre-existing, invisible in every
functional test, and worth 1.4 us a placement. Found by profiling the kernel's
own result and wondering what the rest of the time was.

The unit, end to end: **29.9 -> 15.3 -> 12.8 -> 5.6 -> 4.2 us.** The ceiling:
**312 -> 952** visible placements a frame, 34 -> 105 per exterior cell, and a
player's house from 312 to 952.

### What is left is not the kernel

The kernel measures **0.15 us a placement**. The remaining 4.2 is the Python
either side: a `DrawItem` (1.41 us) and a `Transform` per survivor, both frozen
dataclasses and both contract surfaces. `slots=True` was measured on that shape
and buys nothing - on a frozen dataclass the `object.__setattr__` calls dominate
either way.

So the next reduction is not a faster kernel. It is a different draw list, and
that is a contract change rather than an optimisation. Worth saying plainly so
that nobody spends a week on the kernel again.

### Mutation, in three places

* **The comparator**, at the result level: dropped placements, spurious ones,
  reordering, nudged centres and distances, a translated instance, a rotated
  one, a changed light, a truncated block. Nine, all caught - and the
  truncation found the comparator *raising* on a wrong answer instead of
  reporting it, which would have turned a caught mutant into an error in the
  harness.
* **The wiring**, twenty mutants. Five survived the first pass, all five in
  `_place_payload` - the bridge between the runtime and the kernel. They were
  invisible because the *software* path recomputes from
  `DrawItem.cell_placement` and never reads what the kernel returned, so only
  culling and the packed instances depend on the payload and no test varied
  them. A dropped cell placement, a wrong field of view and a dropped lightmap
  all drew a correct picture.
* **A double-free path in the result**, found by re-reading rather than by any
  test. `Py_BuildValue("{s:N,...}")` steals its values, and when it fails
  partway it releases the ones it has already taken - so the cleanup block
  would have released them a second time. It needs an allocation failure to
  happen at all, which means the one time it fires is the worst possible time.
  Built by hand with `PyDict_SetItemString` now, which takes its own reference
  and leaves one owner throughout.
* **The C itself**, mutated, recompiled and run: composing the wrong way round,
  a row-major matrix, a dropped cell translation, truncation instead of floor
  in the light index, a forgotten aspect ratio, a near plane tested without the
  radius. Six for six, hundreds of divergences each. That is the first time the
  differential suite has been shown to catch a *broken build* rather than a
  broken dict, and it is what D28 was written for.

### And two faults in the tests, one of which failed CI

**A leak test that reported a leak that was not there.** The reference-count
check took its baseline through a list comprehension and its second reading
inside `for obj, was in zip(watched, before)` - and a `zip` keeps its last
result tuple alive to reuse it, so every object read one higher. Uniform, wrong,
and on objects the kernel never touches, which is what gave it away. Both
readings go through the same comprehension now.

**A test that failed the pure-python CI job**, which is the job that exists to
prove the shell needs no accelerator at all. `test_more_than_one_implementation_was_actually_compared`
asserted that there is something to compare the reference against - true
everywhere except the one configuration L7 promises. It is a class-level skip
now, with the reason written out: "there is nothing to compare" is honest on a
machine with no compiler and a real fault anywhere else.

Both were caught by CI and by reading, not by the local suite, because the local
machine has a compiler and numpy. Reproducing that job locally - hiding the
`.pyd` and blocking `numpy` on the meta path - is two lines and should have been
the check before pushing.

---

## D54 — The fourth kernel, and a mutation harness that was testing stale bytecode

`Skeleton.hitboxes` was **61% of a volley** - D45 estimated ~50% and it had
grown as the other halves got faster. `pose_capsules` is that closed.

### The seam, and the constraint that placed it

The kernel takes a root, a flat rest pose, a flat current pose and **the bone
indices that survived the caller's limb filter**, and returns the world-space
capsules for exactly those, packed.

That last argument is the whole design. `conformance`'s docstring already
promised that every seam kernel sits *below* the `limb_state` read, and this is
the first kernel where obeying that took thought: deciding **which** bones offer
a hitbox is a read of limb state and therefore policy (L8), while placing the
bones that survived is arithmetic. So `_surviving_bones` does the reading, in
Python, and hands over integers. A native kernel never sees a limb id or a state
string - and a test asserts that by spying on the payload and checking no limb
id appears anywhere in it.

`None` rather than a full index list when nothing is severed: that is the
overwhelmingly common case, and building a list per rig per volley to say "all
of them" is exactly the marshalling this kernel exists to remove.

### Two caches, and `pose_version` finally has a consumer

The rest pose is fixed per `RegionSet` and is packed once, in `__post_init__`,
beside `extent` - same argument, same place.

The current pose changes only when somebody writes one, and `Skeleton` has had
`pose_version` since it was written, documented as being there "so a consumer
can tell a stale read". This is the first consumer. `pose_rows()` rebuilds only
when the version has moved, which means a standing NPC pays nothing across
frames and a volley pays once per rig rather than once per arrow.

A stale pose cache would be a hit registered on a body that has moved, so four
tests pin the invalidation directly rather than trusting the version bump.

### numpy loses a third time, and that is now a rule

| | python | numpy | native |
|---|---|---|---|
| one 6-bone humanoid | 4.55 us/bone | 33.9 (0.13x) | **0.15 (30x)** |
| 200 bones in one call | 4.52 | 1.80 (2.5x) | **0.05 (90x)** |
| **a volley's 62 rigs** | 2.77 ms | **14.1 ms** | **0.05 ms (57x)** |

NumPy has now won exactly one of four kernels - `segment_query`, the only one
handed everything in a single call - and lost all three that are invoked per
thing. That is not three coincidences, and `KERNEL_PREFERENCE` now says so in
words: **numpy belongs on a kernel called once with everything, and nowhere
else**, measured over a real frame or volley rather than a single big call.
Both times the single-call number said the opposite of the truth.

### The numbers

`Skeleton.hitboxes` 29.9 us -> **3.47 us** on the volley's path, and a 40-arrow
volley over 200 entities **3.05 ms -> 1.71 ms**. `quat_rotate` went from 4,230
calls at the top of the profile to 270 near the bottom.

Where the 3.47 goes: 0.98 in the kernel, the rest in Python marshalling. Two
smaller decisions came out of measuring that:

* **`hitbox_rows`**, a second wrapper that skips `Capsule` entirely. The volley
  called `hitboxes()` and immediately took the capsules apart again into lists
  for `nearest_region` - six objects built per rig per volley purely to be
  discarded. `hitboxes()` still exists and still returns capsules; selection and
  IK use it.
* **`select()` is memoised.** It measured 3.35 us - more than the whole kernel
  for a humanoid - so a lookup per rig would have handed the win straight back.
  The hot path passes the kernel down from the one selection the volley already
  makes; the memoisation is for everyone else. Keyed on `KERNEL_PREFERENCE`'s
  contents, because every cross-implementation test in the suite works by
  swapping that table.

### A divergence found by writing the second implementation

The reference indexed the bone list with plain Python indexing, so a **negative**
index silently wrapped and served the last bone - a hitbox belonging to a
different limb than the caller asked for. numpy would have done the same; the C
raised. No generated case contains a negative index, so the differential suite
would never have found it.

All three refuse it now. Writing a second implementation of a thing is a way of
reading the first one that reading it is not.

### The part worth reading: the harness was testing stale bytecode

Running the wiring mutants left the suite failing in a way that looked exactly
like a real regression - 28 failures, a capsule endpoint at `5.1e-28` - in a
working tree `git status` called clean, with the source visibly correct.

CPython validates a `.pyc` against the source's **(mtime, size)**. The mutation
that swaps `"7d"` for `"7f"` changes neither. Restoring the file inside the same
mtime tick therefore left the *mutant bytecode* valid and importable, and the
next interpreter loaded it.

That is the third defect in this harness, and the worst, because unlike the
other two it corrupts results in **both** directions:

1. it can mask a mutant, reporting SURVIVED for code that is tested;
2. it can leak a mutant into the *next* mutant's run, reporting caught for code
   that is not.

Every write now deletes the module's cached bytecode and stamps the source a
second into the future. The C harness had the same disease one layer down -
setuptools skips the compiler when the object file looks newer than the source,
so a restored `.c` of the same length was never recompiled and the **mutant
binary** stayed on disk. It now stamps the source, checks the rebuild succeeded,
and re-runs the differential suite against the restored kernel before it will
say it finished.

**Every mutation suite in this project was then re-run.** Three verdicts
changed, and each one was worth having:

* `place_batch`'s "the light index truncates instead of flooring" had been
  reported caught with 692 divergences. It is an **equivalent mutant**: the
  clamp to `[0, side-1]` maps every negative index to 0 whichever way the
  division rounds, so the two cannot be told apart. The 692 was contamination.
  The C comment claimed the mutation would break lighting west of the origin;
  it now says the truth, which is that the clamp hides it and `floor` is there
  to mirror the reference expression by expression rather than to fix a bug.
* `pose_capsules`'s bounds check showed 0 divergences, correctly: no *generated*
  payload carries a malformed bone index, because validation is covered by
  ordinary tests. The C harness was judging mutants on the differential suite
  alone; it runs the unit tests too now.
* **`select()`'s cache had no test at all.** The mutation that made it ignore
  `KERNEL_PREFERENCE` had been reported caught, by stale bytecode. It is a real
  hole and a bad one: every cross-implementation comparison in this suite works
  by swapping that table, so a cache that ignored it would have turned all of
  them into comparisons of one implementation with itself, all still green.

### And the same test mistake, twice

The new cache test asserted that swapping the preference returns a *different
function* - true everywhere except the pure-python job, where the reference is
the only implementation and the swap legitimately returns the same one. That is
the second test in two kernels to assume more than one implementation exists.
It asserts on the cache rather than the kernel now.

Twenty-four mutants for this kernel - 17 on the wiring, 7 on the C, all caught -
plus six harness-liveness tests. 774 tests green, in both CI configurations,
checked before pushing this time.
