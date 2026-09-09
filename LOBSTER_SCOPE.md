# Lobster — the Voxel Shell (Scope v0.4)

The 3D counterpart to Tuna: the layer that turns Octopus's topological world
into an actual place you can stand in, swing a sword in, and knock a wall
down in. Same rule Tuna follows — Octopus has no geometry, so Lobster
invents all of it, and nothing Lobster invents leaks back into Octopus.

> **The pitch:** the genre this targets (Morrowind/Oblivion-scale, disc-era
> budget) has already had every one of its hard engineering problems solved,
> publicly, more than once. Lobster's job is to pick the solved solution for
> each one on day one — not because it's clever, but because *not* doing that
> is how a two-person team ends up hand-rolling a streaming infinite-voxel
> engine to make a game that structurally does not need one.

**v0.3 changelog:** renames §7's tiers off Octopus's simulation-tier
vocabulary to avoid a collision that would've become migration work; splits
skeleton pose ownership (Lobster) from animation control (Shrimp); replaces
formation-offset collapsing with leader-leash following (removes a whole bug
class for free); extends the navmesh escape hatch to cover adjacency and the
connection graph; adds the missing world-tick query for structure damage;
locks a provisional exterior cell size; and freezes the zone-shape primitive
set and the spawn/sound lint discipline.

**v0.4 changelog:** locks the spatial index in §7 to a uniform grid (BVH
demoted to a documented fallback, not a live decision); replaces the
author-set `navmesh_load_bearing` flag with build-step auto-inference off
the baked navmesh, removing a silent-bug-by-omission authoring trap; closes
three edge cases — stale spatial-index snapshots for dormant entities under
a PROJECTILE-tier query (§7), an async-mesh-vs-navmesh race on structural
collapse (§10), and an out-of-bounds `destroyed_chunks` index after a mod
shrinks or replaces a structure (§6).

---

## 0. The three decisions this scope is built on

| Decision | Chosen | Why it's the solved-problem choice |
|---|---|---|
| Terrain mutability | **Hybrid** — terrain is static/authored, structures are destructible | Splits one hard problem (general runtime voxel terrain editing + streaming remesh) into one *trivial* problem (static terrain) and one *small, bounded* problem (destructible objects at object scale, not world scale). |
| Tech base | **Custom engine, from scratch** | Matches Octopus's BYOB philosophy. Affordable because a cell-based, mostly-static-voxel renderer is a small, well-documented target, not a general-purpose engine. |
| World structure | **Cell-based, Morrowind/Oblivion-style** | Turns open-world streaming/LOD (hard) into "load this cell, unload that one" (solved), and fits Octopus's Zone model better than seamless streaming would. |

Every section below is downstream of these three rows.

---

## 1. What Lobster actually does

**Two separate voxel systems, not one.** Terrain is authored once, meshed
once at build time. Structures are small, independent voxel grids, meshed at
runtime, and the only thing damage ever touches.

**Cell loading, not streaming.** One cell (plus immediate exterior
neighbors) resident at a time, 1:1 with an Octopus `Location`.

**Hit detection, not damage rules.** Lobster answers "what got hit, how
hard, from what," reports it as an Event, and never contains what a
severed arm *means*.

**Skeleton pose data, not animation control.** Lobster owns bone matrices
and hitboxes; Shrimp's animation controller decides what pose to ask for.

**Tiered, spatially-queried collision** for crowds, not per-entity testing
(§7).

**Baked navigation**, with a declared, bounded escape hatch for destructible
geometry that actually changes what's reachable (§10).

**A renderer sized for the actual target.** Baked per-cell lighting, small
texture atlases or vertex-colored voxels, cell-boundary visibility, distance
fog instead of LOD popping.

---

## 2. Philosophy — the rules everything below obeys

| | |
|---|---|
| **L1** | Every hard problem gets the industry's already-solved answer, chosen on day one. |
| **L2** | Terrain and structures are never the same system — dedicated test in §14. |
| **L3** | A cell is the unit of everything — loading, lighting, navmesh, visibility, budget. |
| **L4** | Lobster reports; it does not decide. |
| **L5** | Damage that must be remembered is an Octopus record, not a Lobster save file. |
| **L6** | Budgets are declared and enforced per cell, not assumed for the whole world. |
| **L7** | Lobster has zero policy. If bloat shows up in a finished build, it should be traceable to content, never to the shell. |
| **L8** | *(new)* **Lobster is the geometry layer, not the behavior layer.** It owns pose data, hitboxes, and movement primitives — never a state machine, a blend tree, or a formation's tactical intent. The instant Lobster starts deciding *what to animate* or *when to flank*, it has quietly become Shrimp. |

---

## 3. Known problems → known solutions

| Problem | Solved answer | Notes |
|---|---|---|
| Turning a voxel grid into a mesh without a triangle explosion | **Greedy meshing** | Build-time for terrain; touched-micro-chunk-only at runtime for structures. |
| Runtime cost of a destructible object | **Micro-chunking** (8³ voxels default) | Cost proportional to hits landed, never structure size. |
| Open-world streaming/LOD | **Don't stream — load cells** | Removes chunk prioritization, popping, seam-hiding from scope. |
| Lighting without a GI solution | **Per-cell baked lightmaps**; structures sample ambient at their position | No per-structure light grid to store or update on damage. |
| Visibility / overdraw | **Cell-boundary culling** | Only resident cells draw. |
| Navigation, common case | **Per-cell baked navmesh** | No runtime regen for static terrain. |
| Navigation, destructible edge case | **Build-step-inferred `navmesh_load_bearing`, local patch recompute extended to affected neighbors** (§10) | Bounded, and no longer an authoring burden — see below. |
| Hit detection on a humanoid | **Per-bone capsule/box hitboxes**, ACTIVE tier only, tested via a **uniform spatial grid** | Tied to a Lobster-owned tier, not Octopus's simulation tier — see §7. |
| Foot placement on uneven terrain | **Procedural IK** on top of skeletal animation | — |
| Group movement (4–12 unit war parties) | **Leader-leash following**, not formation-offset pathfinding (§9) | The StarCraft-era answer; queuing falls out for free. |
| Crowds of 50–200+ entities near a swing/blast | **Spatial query (grid/BVH) → small candidate set → tiered hit test** (§7) | Cost scales with attacks and tiers, not population. |
| Unwitnessed mass-casualty events | **World tick resolves once, writes the outcome, cell load applies it cheaply** (§6.5) | No "calculate on approach" path. |
| Where do ambient sound sources live | **Cell metadata**, `{position, sound_id, radius}` | A mod-supplied list is ordinary layered content — same merge/quarantine rules as any other record, never a second save channel (§4.5). |

---

## 4. How Octopus's topology becomes Lobster's geometry

- **`Location` → Cell**, 1:1.
- **`connections` → doors/portals; the connection owns the spawn point.**
  Matches Oblivion's pattern; lets two doors into the same room arrive at
  different spots. Every Location also carries a `default_spawn_transform`
  for entries not through an authored connection (fast travel, first-time
  dungeon entry). **Lint discipline, extended in v0.3:** the same build-step
  check that already flags a one-way connection (D26) also flags a Location
  with **no `default_spawn_transform` and no incoming connection carrying a
  spawn point** — an unenterable Location should be a build-time error, not
  a runtime surprise the first time someone fast-travels there.
- **`Zone` → trigger volume**, shape via `shape_ref`: `box`, `cylinder`, or
  `polygon`. **These three primitives are frozen as of this scope.** They're
  sufficient for every anticipated case; adding a fourth later is a
  deliberate decision with its own note, not a one-line addition — every new
  primitive is a new code path content can come to depend on.
- **What Lobster does not invent:** occupancy, scheduling, faction state —
  `queries.zone_occupants` already answers that.

---

## 4.5 Content pipeline

- **Authoring format:** MagicaVoxel `.vox` for structures and terrain
  chunks.
- **Build step:** `lobster-build` consumes a world manifest (cells,
  connections + spawn transforms, zone shapes) plus per-cell `.vox` files,
  produces one `.lobster_cell` bundle per Location — baked terrain mesh,
  baked navmesh, baked lightmap, undamaged structure definitions, prop and
  **sound metadata**. Any mod-supplied addition to any of this — including a
  sound source list — goes through the same operation-log/merge/quarantine
  path as any other package content; the build step must not special-case it
  into a bypass channel.
- **Runtime bundle:** one `.lobster_cell` per Location id.
- **Hot reload: not supported in v1.** Rebuild + relaunch, stated plainly.

---

## 5. The combat and animation boundary, precisely

| | Owns |
|---|---|
| **Lobster** | Swing volumes, hitbox regions (ACTIVE tier), "region X hit for Y force," structure voxel damage + remesh, **skeleton pose data** (bone matrices) exposed via a `set_pose` interface, reading live limb state **only to decide which hitboxes exist for the next test**. |
| **Shrimp — animation controller** | The state machine: which clip plays, blend weights, when to transition to ragdoll, what "disabled arm" looks like. Reads the **same** `limb_state` query Lobster uses, independently, to make those decisions. |
| **Octopus (core + `stats` module)** | HP, per-limb damage state, thresholds, Effects/ActiveEffects, the Event chain. |
| **Shrimp — content** | What "arm disabled" *means* gameplay-wise, dismemberment thresholds, which hit-region set a creature type uses. |

**The live limb-state contract (tightened in v0.3):** `Character.limb_state`
is a `LimbId → "intact" | "disabled" | "severed"` map, written via
`patch_record` on threshold crossings, `REPLACE_PER_SUBFIELD` per limb.
Lobster reads it in exactly one place: **before resolving a hit-test**, so a
severed limb offers no hitbox. **Lobster does not read this to drive
animation or pose changes** — that would put it in the animation business,
which §1/L8 explicitly rules out. Shrimp's animation controller reads the
identical field, via the identical query, and decides what to do with it.
Both systems reading the same source of truth independently is the correct
boundary — not Lobster reading it once and pushing a decision downstream.

**Cross-repo round-trip test:** sever a limb → Lobster's next hit-test
correctly finds no hitbox on the stump *and*, independently, Shrimp's
animation controller correctly stops posing that limb. Two assertions, two
systems, one shared field — the test should exercise both, not assume one
implies the other.

**Hit-region vocabulary** stays a Lobster-owned enum (6 humanoid regions);
non-humanoid creatures get their own declared region sets at content-time,
the mechanism stays Lobster's.

---

## 6. Structure voxel data — the exact wire format

**1. The authored grid — content, ships in the bundle, never saved:**

```
StructureVoxelData:
  structure_id:  stable id, assigned at authoring time
  grid_size:     u16  (multiple of chunk_size)
  chunk_size:    u8   (default 8)
  material_ids:  []u8, length grid_size³ (palette index, not per-voxel color)
  origin:        position + rotation within the cell
```

**2. The break-state — the one thing that's actually save data:**

```
StructureState (Octopus record):
  id:               matches structure_id above
  location_id:      which cell/Location this structure belongs to
  destroyed_chunks: Set<u16>   (chunk indices, merge policy UNION_TOMBSTONED)
```

`UNION_TOMBSTONED` over `REPLACE_PER_SUBFIELD`: destruction is additive and
monotonic — two mods damaging different walls of the same keep should both
apply. Repair is the rare explicit act, modeled as its own tombstone
reversal, not a second merge semantics.

At cell load, Lobster reads `destroyed_chunks`, masks the authored grid, and
meshes only the affected micro-chunks. The dedicated round-trip test (save →
mod removal/quarantine → reinstall → break-state intact) lives in §14.

**Bounds check on load (v0.4).** `UNION_TOMBSTONED` handles the additive
case cleanly, but says nothing about a mod that removes a structure or
replaces `structure_id` with a smaller authored grid — a saved
`destroyed_chunks` set built against the old `grid_size` can contain indices
that are out of range for the new one. Lobster must not hand those straight
to the mesher: on load, compute `max(destroyed_chunks)` against the current
`grid_size³` / `chunk_size³` chunk count, and if any index is out of bounds,
**quarantine those specific indices and log the reason**, same as Octopus's
own quarantine behavior for unresolvable save ops — never crash the mesh
builder, and never silently drop the whole record when only a few indices
are stale.

---

## 6.5 Outcome calculation vs. visual aftermath

**There is no "calculate on approach" path.**

- **Witnessed outcomes** resolve in real time through §7's tiered
  collision/zone-occupancy path.
- **Unwitnessed outcomes** resolve once, at event time, via Octopus's world
  tick: the Event queries the Zone's occupancy index for NPCs, and —
  **closing the v0.2 gap** — queries `structures_in_zone` /
  `structures_in_location` (§13) for any structures in range, applying
  damage to each `StructureState.destroyed_chunks` the same way a
  player-witnessed hit would.
- **Cell load applies the pre-computed result** — the break-state read in §6
  already covers this; a town fireballed while the player was elsewhere
  shows up already-rubbled on next entry, no re-simulation, no stutter.

Cost is paid exactly once, by whichever path actually observed the event.

---

## 7. Crowd collision and hit-test tiering

Spatial aggregation, not per-entity testing: every entity's position lives
in a per-cell spatial index; a swing or blast queries it for a small
candidate set before any detailed hitbox test runs.

**Spatial index: uniform grid, locked as the default (v0.4).** A BVH is the
general-purpose answer, but Lobster's world isn't general-purpose — it's
cell-bounded (§0), the max entity count per cell is a declared budget
(§14), and entity distribution is relatively stable frame-to-frame (NPCs
follow schedules, players move along paths, nothing chaotic). A uniform
grid tuned to average entity spacing (proposal: **2.5 m**, adjustable
per-cell) gives O(1) insertion and O(1) query with none of a BVH's
tree-balancing overhead. At a 128 m cell (§14), that's roughly a 51×51 grid
of simple linked-list/small-vector buckets — memory cost is negligible even
for a dense market square, because the grid itself is tiny relative to the
cell. **BVH is explicitly not the default; it's a documented fallback** if
profiling ever shows a specific cell's distribution defeating the grid's
fixed resolution (a genuinely pathological case, not the expected one).
Locking this now removes a "which one do I implement" ambiguity for
whoever builds this next.

**Stale-snapshot risk at PROJECTILE-tier range (v0.4).** A fast-moving
projectile can query the spatial index for a cell Octopus considers dormant
— but a dormant entity's transform in that grid may not have been touched
since the cell was last resident, because DORMANT-tier entities aren't
ticked for position the way ACTIVE ones are. A pure geometric capsule/ray
query against a stale node is wrong in a way that's easy to miss in testing
(it'll usually still be "close enough," until it isn't). **Fix:** the
per-cell spatial index takes a transform snapshot on cell entry and on cell
exit — not continuously for dormant entities — so a PROJECTILE-tier query
is always testing against the last *known-correct* position rather than
whatever stale value happened to be sitting in the grid. This keeps the
index cheap (no per-frame updates for entities nobody's simulating) while
keeping hit-tests honest.

**Tier names, deliberately Lobster's own — not assumed to match Octopus's
simulation tiers.** v0.2 borrowed ACTIVE/NEARBY/DORMANT from a secondhand
description of Octopus's simulation model without confirming it, which is
exactly the kind of parallel-vocabulary drift that turns into migration work
later. **This still needs a direct check against `nested-object-engine-sds.md`
before any code lands** — if Octopus's real simulation tiers share a name
with the tier below, adopt that name for the shared concept (the ACTIVE
case genuinely is the same thing in both systems). The middle tier stays
Lobster-specific regardless, because it answers a different question:

| Lobster tier | Scope | Hitbox fidelity | Relationship to Octopus's simulation state |
|---|---|---|---|
| **ACTIVE** | Current cell + immediate melee range | Full skeleton, 6-region hit-test | Same concept as Octopus's actively-simulated tier, once confirmed |
| **PROJECTILE** | Any cell within current spell/projectile range, regardless of connection-graph adjacency | Single whole-body capsule | May span cells Octopus considers simulated *or* dormant — Lobster is not promoting simulation state, only flagging a geometry candidate for a hit-test |
| **DORMANT** | Everything else resident | None — any effect goes through §6.5's occupancy+Event path | Octopus's own dormant/unresolved state, unchanged |

The distinction that matters: a fireball two connections away can hit an
NPC that Octopus still considers dormant. That NPC isn't promoted to active
simulation by being hit — it just receives an Effect via the ordinary Event
path, the same as it would from an unwitnessed world-tick event. **PROJECTILE
is a hit-test-candidacy tier, not a simulation-promotion tier**, and the two
systems should never be conflated.

**Mass-casualty events:** resolving "which of 40 villagers in a blast
radius got hit" is a zone-occupancy query + `apply_effect` per occupant —
the same §6.5 machinery, triggered immediately instead of on a tick
boundary. Lobster's job is presentational: ragdoll whichever occupants are
currently ACTIVE/PROJECTILE tier, run the ordinary structure-damage path on
micro-chunks in range. No 40-body physics simulation required.

---

## 8. UI and world-interaction ownership

| | Owns |
|---|---|
| **Lobster** | Object selection/raycasting, world-space labels, physically placing/removing an item's representation in the world. |
| **Shrimp** | Inventory screens, equip menus, dialogue UI, all 2D chrome. |
| **Octopus** | Inventory data — `Item` records, `queries.resolve_equipment_slots`. |

---

## 9. War-party movement: leader-leash, not formation pathfinding

**v0.3 replaces v0.2's "formation offsets that collapse to single-file"
with leader-leash following**, the answer every RTS since StarCraft has
used, because the collapse behavior isn't special-case logic — it's the
natural output of a simpler rule:

- The **leader** pathfinds to the actual destination.
- **Followers do not pathfind to the destination.** Each follower pathfinds
  toward the leader's current position plus a small formation offset.
- If a follower can't reach its offset (blocked by terrain, a doorway,
  another entity), it **waits and retries next frame** rather than forcing
  the offset.
- This naturally produces a queue: through a bottleneck, the first follower
  reaches the leader, the second reaches the first, and so on — with zero
  formation-specific bottleneck-detection code.
- **Formation types** (`line`, `wedge`, `circle`, `scatter`) are just the
  offset pattern a war party's `AIPackage` selects; the leash mechanism
  underneath is identical regardless of which pattern is chosen.
- **Engagement, target selection, holding position, flanking** stay
  entirely out of Lobster — `AIPackage` content on top of "follow the leader
  with this offset pattern," per L8.

This removes an entire bug class (followers stuck trying to hold a
geometrically-impossible offset) without any bottleneck-specific code.

---

## 10. Navmesh vs. broken structures — extended for adjacency

**v0.2's rule (an author-set `navmesh_load_bearing` flag triggers a local
recompute) undercounted its own blast radius, and had a second, quieter
problem: it made silence the default failure mode.** A collapsed gatehouse
doesn't just change the cell it's in — it changes what's *reachable from*
the adjacent cell too, and it may sever a `connections` edge entirely, which
is an Octopus-side change, not just a Lobster one. And relying on an author
to remember to flag every doorway/gate/bridge chunk means the actual bug —
NPCs walking through rubble that should now block them — produces no error,
no warning, just quietly wrong behavior the first time someone forgets.

**Updated rule (v0.4): the flag is inferred by the build step, not
authored by hand.** Bethesda's navmesh tooling already solved exactly this
— opt authors *out* of the rare decorative case, rather than opt them *in*
to the common critical one:

1. `lobster-build` (§4.5) bakes the cell's navmesh as usual, then, for every
   micro-chunk in every structure in that cell, tests whether the chunk's
   bounding volume intersects any navmesh polygon. If it does, the chunk is
   automatically flagged `navmesh_load_bearing: true`; if not, `false`. No
   authoring step required for the common case.
2. Authors may still **override** the inferred flag on a specific chunk
   (e.g., a decorative arch that geometrically intersects the navmesh but
   was never meant to block anything, or a gate that's deliberately
   destructible-and-path-changing) — but the default now fails safe instead
   of failing silent.
3. Destroying a `navmesh_load_bearing` chunk (inferred or overridden)
   triggers a local navmesh patch recompute for that cell **and any
   adjacent cell whose connections into this cell are affected** — still
   bounded (cell + neighbors matches the same residency rule as §4).
4. If the destruction is severe enough to sever a `connections` edge
   entirely (the gatehouse *was* the only way through), that's a
   `patch_record` against the Location's `connections` field, going through
   Octopus's ordinary record-update path and triggering its normal
   re-resolution.
5. These are two different systems (Lobster's navmesh, Octopus's connection
   graph) that both need to react to the same destruction event; a test
   should confirm the graph and the navmesh never disagree about whether a
   path exists.

Destroying a chunk the build step correctly inferred as non-load-bearing
still never touches either system.

**The synchronous/asynchronous split (v0.4).** Steps 3–5 above take real
time — a navmesh patch recompute, and possibly a connection-graph
re-resolution, are not free within a single frame. That opens a window,
sometimes several frames wide, where the *visual* micro-chunk mesh has
already updated (the wall looks broken) but the *pathing* graph hasn't
caught up yet. Leaving this implicit invites a race: an NPC's leader-leash
target (§9) computed against the old navmesh, one frame after the geometry
changed. **The rule:** the physical voxel collider updates **synchronously**,
on the exact frame of impact — a player or NPC standing in the rubble the
instant it forms must not fall through or clip. The navmesh patch and any
connection-graph patch are allowed to **defer asynchronously**. While a
cell's navmesh node is marked dirty/pending, any AI whose current path or
leash target passes through that node **holds position** rather than
committing to a route that might be invalidated a frame later. This is the
same instinct as Octopus's own incremental-resolution guarantee — a
consumer should never see a torn, half-updated view of the world, even if
producing the fully-updated view takes a few extra frames.

---

## 11. Radiant quests — the one Shrimp constraint worth flagging now

Radiant quest generation must query the **live** world graph — which cells
and connections currently exist (including any severed by §10's case),
who's currently alive — rather than assume a static content set, since mods
and in-world destruction can both change it.

---

## 12. What Lobster deliberately does not do

- Infinite/procedural world generation.
- Runtime terrain editing.
- Damage math, HP, limb-disability rules, healing.
- Dialogue, quests, faction/reputation logic, scheduling.
- **Animation state, blend trees, ragdoll transition decisions** (L8 — new
  emphasis in v0.3; Lobster supplies pose data and a `set_pose` interface,
  nothing upstream of that).
- **Tactical AI: target selection, flanking, holding a position** (§9).
- Multiplayer/netcode.
- World/content authoring tools — a separate repo, fed by what Cuttlefish
  and Shrimp reveal writers actually struggle with.
- Any authoring intelligence — no auto-invented return connections, no
  "smart" auto-LOD overriding what a writer authored.

---

## 13. The cheap contract (Lobster↔Octopus, draft)

**Events Lobster fires:** `on_enter_cell(location_id)`,
`on_exit_cell(location_id)`, `on_hit_location(target_id, region, force,
source_id)`, `on_structure_damaged(structure_id, chunk_indices[])`,
`on_interact(target_id)`, `on_item_placed`/`on_item_removed(item_id,
cell_id, transform)`.

**The one record Lobster's save-integration depends on:**
`StructureState{id, location_id, destroyed_chunks: Set<u16>}`,
`UNION_TOMBSTONED`.

**Live queries Lobster is permitted to call**, all pure, never cached beyond
the current hit-test or frame: `queries.resolve_npc_state`,
`queries.zone_occupants`, `queries.resolve_equipment_slots`,
`queries.list_active_effects`, the `limb_state` read (§5, hit-testing only),
and — **new in v0.3, closing the §6.5 gap** — `queries.structures_in_zone
(zone_id)` and `queries.structures_in_location(location_id)`, both pure
lookups over resolved state, same cost model as the occupancy query
(O(structures in the zone), not O(total structures in the world)). These are
what let the world tick apply mass-casualty structure damage without
Lobster being loaded or involved at all.

**Declared invariants a writer can rely on:**
1. Geometry is data — no private save format anywhere, including a mod's
   sound-source list (§4.5).
2. Failure is visible and attributable — over-budget content names the
   specific record, the specific cell.
3. No hidden coupling to a specific game's content.

---

## 14. Budgets and tests to lock before content lands

**Budgets:**
- Structure voxel resolution: ~0.25 m/voxel (proposal, unchanged).
- Structure micro-chunk size: 8³ voxels (proposal, unchanged).
- **Exterior cell size: 128 m × 128 m, provisional but locked as a
  first-class constant as of v0.3** — big enough that a hamlet fits in one
  to a few cells, small enough that "cell + 1 ring" residency stays
  reasonable. Treat as changeable with a note, not as an open variable
  blocking everything downstream any longer.
- Peak resident cells + peak memory during a transition fade: needs its own
  ceiling and CI assertion.
- Max ACTIVE-tier (full-skeleton) NPCs simultaneously resident — a function
  of the now-locked cell size, worth a hard number next.

**Tests:**
1. Terrain/structure separation — never share an object or memory pool.
2. Structure-break round trip — save → mod removal (quarantine) → reinstall
   → break-state intact.
3. **Split limb-state round trip** (v0.3) — Lobster's hit-test correctly
   loses the hitbox on sever; Shrimp's animation controller, independently,
   correctly stops posing that limb. Two assertions, not one.
4. Cell-transition peak-memory budget, in CI.
5. **Navmesh/connection-graph agreement** (v0.3, new) — after a severed
   connection, the navmesh and the connection graph never disagree about
   whether a path exists.
6. **Leader-leash queuing** (v0.3, new) — a war party through a single-file
   bottleneck never gets a follower stuck; the queue forms with zero
   formation-specific logic.
7. **PROJECTILE-tier stale-snapshot check** (v0.4, new) — a projectile fired
   at a dormant entity that hasn't moved since cell entry still resolves
   against its last known-correct transform, not a garbage/never-set value.
8. **Sync-collider/async-navmesh race** (v0.4, new) — an NPC's leash target
   never routes through a cell whose navmesh is marked dirty/pending; the
   physical collider is provably updated on the same frame as the visual
   mesh, every time, regardless of how long the navmesh patch takes.
9. **Out-of-bounds break-state quarantine** (v0.4, new) — loading a
   `StructureState` whose `destroyed_chunks` no longer fit the current
   `grid_size` quarantines only the stale indices and logs why, and never
   crashes the mesh builder or discards the whole record.

---

## 15. Things that will bite you

1. Don't let terrain and structures share a code path.
2. A connection without a return door — or a Location without any spawn
   path — is unauthored content, and the build step should say so loudly
   (§4).
3. Structure damage that isn't `StructureState` will desync the moment a
   mod changes that building.
4. **A hitbox is only correct if limb-state is read live at hit-test time —
   but don't let that turn into Lobster also driving animation from the
   same read** (§5/L8). That's the new failure mode to watch for in v0.3:
   the fix for the old bug (stale hitboxes) shouldn't grow into scope creep
   (Lobster becoming an animation controller).
5. Cell transition is a budget moment, not a loading screen.
6. A destroyed decorative chunk should never cost a navmesh recompute — but
   a destroyed load-bearing one might cost *two* cells' worth, plus a
   connection-graph patch. Don't budget for one when the real cost is the
   other (§10).
7. **If `navmesh_load_bearing` ever goes back to being an author-set flag
   instead of a build-step inference, that's a regression** — it reopens
   the exact silent-failure mode (a forgotten flag on a gatehouse, NPCs
   walking through rubble with no error) that v0.4 exists to close.
8. **A uniform grid tuned for the common case can still be defeated by an
   outlier cell** (a siege with 80 NPCs packed into a 15m courtyard). That's
   the profiling trigger for the BVH fallback in §7 — not a reason to build
   the BVH path preemptively, just a reason to actually profile before
   assuming the default holds everywhere.
10. **A dormant entity's spatial-index entry is only as good as its last
   snapshot.** If a future change adds a code path that moves a dormant
   entity (a scripted relocation, a mod effect) without also refreshing its
   grid snapshot, PROJECTILE-tier queries silently go stale again — the same
   failure mode §7's fix exists to close, reopened from a different angle.
11. **Any shortcut that makes the voxel collider update wait on the navmesh
   patch (or vice versa) reintroduces the §10 race** — collision is always
   synchronous with the visual mesh; pathing is allowed to lag behind it,
   never the other way around.
9. **Formation offsets that get treated as hard constraints, instead of
   leash targets, will get followers stuck in doorways.** If a future
   change reintroduces "pathfind to formation position" instead of
   "pathfind toward the leader," that's a regression back to the bug §9
   exists to avoid.

---

## 16. Open questions to carry into Shrimp scoping

- Confirm Octopus's real simulation-tier names in `nested-object-engine-sds.md`
  and reconcile with §7's ACTIVE/PROJECTILE/DORMANT before code lands.
- Sanity-check the 128 m × 128 m provisional cell size against actual hamlet
  layouts once the first one is blocked out.
- Which non-humanoid creature types need their own hit-region sets, and
  which can share one (quadruped vs. giant spider, etc.).
- Validate the build step's `navmesh_load_bearing` inference (§10) against
  the first few authored cells — confirm it correctly catches every
  gate/drawbridge on a critical path and doesn't over-flag purely decorative
  geometry that happens to graze the navmesh.
- Whether 2.5 m is the right default grid cell size for the spatial index
  (§7) once real NPC density in a market square or a war-party skirmish is
  known, or whether it needs to vary per-cell.
- Formation-type-per-war-party-type mapping (goblin raid → `scatter`, orc
  warband → `wedge`?) — content, but visible in the first playable build.
