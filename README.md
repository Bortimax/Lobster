# Lobster — the Voxel Shell

The geometry layer for [Octopus](../Octopus). Octopus has no geometry; Lobster
invents all of it, and nothing Lobster invents leaks back.

`LOBSTER_SCOPE.md` is the source of truth for this repository. `DECISIONS.md`
records every ambiguity that came up while building it, what the Scope says
about it, and what was chosen. `CONTRACT.md` is the delivery surface Shrimp and
the authoring tool bind against.

Python 3.11, `unittest`. **Core Lobster has no third-party dependency** and
every test runs on a machine with no GPU and no display; a GPU render backend
is used when one is installed (DECISIONS.md D22).

## Quick start

```bash
export LOBSTER_OCTOPUS_PATH=/path/to/Octopus     # or keep it at ../Octopus
python -m unittest discover -s tests -t .
```

```bash
python -m lobster.cli contract                    # the §13 surface, as JSON
python -m lobster.cli build world.manifest.json --out cells
python -m lobster.cli load cell-village --cells cells --packages world.json
python -m lobster.cli damage cell-village keep-gatehouse 12 --cells cells
python -m lobster.cli budgets --cells cells
python -m lobster.cli render cell-village --cells cells --out shot.png
```

## What it does

| | |
|---|---|
| **Two voxel systems, never one** | Terrain is authored and meshed at build time. Structures are small independent grids, meshed at runtime, and the only thing damage ever touches. They share no type, no mesher, no memory pool (L2). |
| **Cell loading, not streaming** | One cell plus its exterior ring, 1:1 with an Octopus `Location`. Adjacency comes from `Location.connections`, not from a coordinate grid Lobster invents. |
| **Hit detection, not damage rules** | "Region X hit for Y force, from Z." What a severed arm *means* lives in Octopus's stats module and Shrimp's content. |
| **Pose data, not animation control** | `set_pose` and bone matrices. No state machine, no blend tree, no ragdoll decision (L8). |
| **Tiered spatial hit-testing** | Uniform grid → small candidate set → per-tier fidelity. Cost scales with attacks, not population. |
| **Baked navigation** | Per-cell navmesh, with a bounded escape hatch for destruction that actually changes what is reachable. |
| **A renderer sized for the target** | Frustum + cell-boundary culling into a draw list, drawn by a backend: the GPU when it is there, a stdlib software rasteriser when it is not. |

## Layout

```
lobster/
  constants.py        declared budgets and locked dimensions
  budgets.py          Budget, MemoryLedger — declared, enforced, attributable
  geometry.py         Vec3, Transform, AABB, Capsule; pure math
  events.py           the seven contract Events + the Octopus sink
  octopus_bridge.py   THE only import site for `lce`; FrameView, permitted queries
  tiers.py            ACTIVE / PROJECTILE / DORMANT, checked against lce.npc
  terrain.py          static, authored, immutable — no mutation API at all
  structures.py       StructureVoxelData (authored) + LiveStructure (break-state)
  structure_mesher.py runtime greedy meshing, per micro-chunk
  structure_state.py  the one save-integrated record, as operations
  bundle.py           .lobster_cell reader (there is no runtime writer)
  cell.py             residency, transitions, the damage entry point
  spatial.py          uniform grid + the snapshot rules
  skeleton.py         bones, hitboxes, set_pose
  hittest.py          the only limb_state reader in the repository
  navmesh.py          baked navmesh, recompute, deferred PatchQueue
  connection_graph.py severing a connection through Octopus's ordinary path
  movement.py         leader-leash following
  zones.py            box / cylinder / polygon trigger volumes (frozen)
  sound.py            ambient sources, read live off the Location record
  ik.py               procedural foot placement, applied only when asked
  worldtick.py        unwitnessed blasts, resolved from bundle headers
  camera.py           camera, projection, frustum culling
  visibility.py       cell-boundary culling and the draw list
  render/             backends: GPU when available, software reference always
  cli.py              build / lint / inspect / load / damage / budgets / contract / test
  build/              lobster-build: vox, meshing, navmesh bake, lighting, lint
packages/             Lobster's two Octopus schema-extension packages
tests/                the §14 suite, plus the contract and the build step
```

## The §14 tests

All nine, all green.

| # | What it holds | File |
|---|---|---|
| 1 | terrain/structure separation — no shared object or pool | `test_separation.py` |
| 2 | break-state round trip: save → mod removal → reinstall | `test_break_state.py` |
| 3 | split limb-state round trip — **two** assertions, two systems | `test_limb_state.py` |
| 4 | cell-transition peak memory, asserted in CI | `test_cell_budget.py` |
| 5 | navmesh / connection-graph agreement after a severed connection | `test_navmesh_agreement.py` |
| 6 | leader-leash queuing with zero formation-specific logic | `test_leader_leash.py` |
| 7 | PROJECTILE-tier stale snapshot | `test_projectile_snapshot.py` |
| 8 | sync collider / async navmesh race | `test_sync_async_race.py` |
| 9 | out-of-bounds break-state quarantine | `test_break_state.py` |

Plus `test_contract.py` (the §13 surface itself), `test_build.py` (the build
step and its lint) and `test_presentation.py` (zones, sound, IK, world tick).

## Integrating

Lobster ships two Octopus content packages. Load them before your world:

- `packages/lobster_geometry.json` — `StructureState`, plus
  `Location.default_spawn_transform`, `Location.lobster_budget`,
  `Location.sound_sources`, `Location.spatial_grid_cell_m`, `Zone.shape`,
  `Zone.shape_location_ref`. Field semantics are in `CONTRACT.md` §7 — in
  particular, `Zone.shape_location_ref` names the cell whose origin a zone
  shape's coordinates are measured from, and you want to set it on any Zone
  spanning more than one Location.
- `packages/lobster_limb_state.json` — `Character.limb_state`. Kept separate so
  a project whose Shrimp or Octopus layer owns that declaration can drop
  Lobster's copy. Its absence means "all limbs intact".

Then:

```python
from lobster.cell import CellManager
from lobster.events import EventBus, OctopusEventSink
from lobster.octopus_bridge import OctopusBridge

bus = EventBus()
bridge = OctopusBridge(session)
manager = CellManager("cells", bus=bus,
                      sink=OctopusEventSink(session).attach(bus))

with bridge.frame() as view:
    cell = manager.set_player_cell(view, "cell-village")
    cell.place("npc-ada", (10.0, 0.0, 4.0), ACTIVE, skeleton=skeleton)
```

Occupancy is not Lobster's to decide — ask `view.zone_occupants(...)` who is
there, then tell Lobster where to draw them.

## What is not built yet

Not the same list as the one below it. These are **staged**, in the order the
project owner ranked them — everything under "deliberately does not do" is
refused instead.

1. **Characters are impostors.** Terrain, structures, props and items all draw
   as real geometry. Entities do not: the software path draws one flat capsule
   per bone, the GPU path a single 1.8 m box. The rig underneath is real — it
   poses, it hit-tests to a limb — but nothing connects a `Model` to a bone.
   **This is the one that would be noticed first in a screenshot**, and it is
   not the skinned-mesh scope: rigid per-bone voxel models need no vertex
   weights and reuse the pose that already exists.
2. **A model's voxel scale** — ASSET_SCOPE §7 step 2a. Props inherit a
   *structure's* 0.25 m, so a candlestick bakes as a 1x4x1 fencepost. Small and
   self-contained; the mesher already takes the argument (D56).
3. **Textured voxels.** Colour-only voxels are the current ask because there is
   no texture artist, and they are sufficient — but tiles are wanted before
   this is over, and **this needs a scope before props are commissioned, not
   before code is written**: textures and voxel resolution pull in opposite
   directions, so art authored for flat colour is authored at the wrong
   resolution for tiles. Ranked above the handset question by the owner.
4. **A handset target.** RENDER_SCOPE §1 keeps the draw model inside a GL ES 3.0
   subset so a phone renderer reuses the shaders, but Python-plus-ModernGL is
   not an Android deployment and D26's language decision would reopen.

Skinned or conventional meshes for characters remain **refused**, not staged —
ASSET_SCOPE §1 and §5.

## What it deliberately does not do

Runtime terrain editing. Damage math, HP, limb-disability rules. Animation state
machines, blend trees, ragdoll transitions. Target selection, flanking, holding
position. Dialogue, quests, factions, scheduling, inventory UI. Infinite or
procedural generation. Multiplayer. Authoring tools. Hot reload. A second save
format.

If any of those turns up in this repository, it is a defect, not a feature.
