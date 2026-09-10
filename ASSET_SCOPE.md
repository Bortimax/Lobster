# Asset scope — making `model_ref` mean something

**Status: scoped, not built.** Items and props reach the draw list as bounded
impostors with no mesh, and `Model.asset_ref` resolves to nothing. This is the
document for closing that.

> **A renderer sized for the actual target.** Baked per-cell lighting, small
> texture atlases or **vertex-colored voxels**, cell-boundary visibility,
> distance fog instead of LOD popping.
> — LOBSTER_SCOPE §1

> **Authoring format:** MagicaVoxel `.vox` for structures and terrain chunks.
> — LOBSTER_SCOPE §4.5

Where this document and `LOBSTER_SCOPE.md` disagree, the Scope wins.

---

## 0. What scoping found

**Most of the pipeline already exists**, in pieces that have never been
connected. That was not obvious from "there is no asset pipeline", which is how
five earlier entries described it — including three of mine.

| link | state |
|---|---|
| an authoring format | **`lobster/build/vox.py`** reads MagicaVoxel `.vox`, by hand, no dependency |
| voxels → mesh | **`StructureMesher`**, greedy, per micro-chunk |
| a record to point at | Octopus **`Model`**: `asset_ref`, `rig_ref`, `lod_refs`, `cost_estimate` |
| a reference from content | `Item.model_ref`, and `PropPlacement.model_ref` in the build manifest |
| somewhere to put it | `.lobster_cell` bundles, and a build step that already reads `.vox` per cell |
| something to draw it | `ModernGLBackend`, with a per-cell model matrix already in the shader |

**Three links are missing**, and only three:

1. nothing resolves `Model.asset_ref` to a file;
2. nothing bakes a *shared* model into a build artifact (structures are baked
   per cell, which is right for them and wrong for a barrel);
3. nothing draws one — props and items are impostors.

So this is not "write a glTF loader". It is joining a chain that is already
five-sixths built, in an idiom the project already uses.

---

## 1. The format: `.vox`, vertex colours, and no textures at all

**Decided by the Scope, not by me.** §4.5 names MagicaVoxel `.vox` as *the*
authoring format, and §1 offers "small texture atlases **or** vertex-colored
voxels" — the `or` is a choice, and `.vox` makes it for us: a `.vox` file
carries a 256-entry palette, so every voxel already has a colour.

Consequences worth stating, because each removes a subsystem:

* **No texture pipeline.** No atlas packer, no UVs, no samplers, no mipmaps, no
  texture memory budget. The GL shader already takes a per-vertex tint and
  needs no change.
* **No importer for anything else.** No glTF, no OBJ, no FBX. A format is a
  contract with content authors and a second one doubles it.
* **The same mesher.** A prop is voxels; `StructureMesher` already turns voxels
  into greedy-meshed quads with palette materials. A second mesher would be the
  thing L2 forbids for terrain and structures, without even L2's excuse.

**Open question for the project owner.** This makes every prop and item a voxel
model. If the intent was ever "characters and swords are conventional meshes,
terrain is voxels", that is a different project and this scope is wrong.

---

## 2. Where a model lives: a shared library, not a cell

Structures bake **into** the cell that contains them, which is right: a gatehouse
is one object in one place, and its break-state is per-instance.

Props and items are the opposite. Fifty barrels in a town are fifty *placements*
of one model, and baking the mesh fifty times would multiply a cell's bytes by
its decoration.

**Proposal: one `models.lobster_lib`, built alongside the cells**, holding each
distinct model's meshed geometry once, keyed by `Model` record id. Cells
continue to carry *placements* only — which is exactly what `PropPlacement` and
`Item.world_transform` already are.

That makes the library a **new build artifact**, and D1 applies to it unchanged:
derived, reproducible from content, never authoritative, safe to delete.

### The residency question this raises

A cell's budget is per cell (L6). A shared library is not. Two options:

* **Load the whole library once** — simple, and bounded by total distinct
  models rather than by residency. Wrong if content ships thousands.
* **Load per residency**, reference-counted across resident cells — bounded,
  and it composes with `GpuResidency`, which already tracks exactly this shape
  of lifetime.

**Recommend the second**, because it is the one that keeps L6 meaningful, and
because the machinery exists: `GpuResidency` already uploads and releases per
cell and already has a `drift()` check for exactly the kind of leak a
reference count invites.

---

## 3. Who resolves a ref to a file: the build step, and only it

`Model.asset_ref` is a string. Something must turn it into
`art/barrel.vox`.

**That something is `lobster-build`, never the runtime.** CONTRACT §5
invariant 1 is asserted by a test: nothing in `lobster/` opens a file for
writing, and the runtime does not read content files either — it reads bundles.
The manifest already carries `vox_dir` and per-structure `vox` paths, so the
resolution rule is the one that exists:

```json
{"vox_dir": "art",
 "models": [{"model_ref": "model-barrel", "vox": "barrel.vox"}]}
```

A `model_ref` with no manifest entry, or a manifest entry with no file, is a
**build error naming both** — the same shape as `item_transform_without_location`
(D33). An unresolvable model at runtime is a thing that does not appear, and
this project's whole posture is that such things fail at build time instead.

---

## 4. Drawing many of one thing: instancing

Fifty barrels is one mesh and fifty transforms. The GL backend already sets a
per-cell `model` matrix; instancing generalises that to a per-instance one.

ES 3.0 has instanced drawing, so RENDER_SCOPE §1's feature baseline permits it
— it is named there as the one exception ("instancing only where ES 3.0 has
it"). The software backend does not instance and does not need to: it loops,
which is what it already does.

**The impostor path stays** for anything with no model, which after this is
still every entity. It is not a placeholder to be removed; it is what a thing
with no mesh looks like.

---

## 5. Two conflicts with Octopus's `Model` record

Both are real, both are resolved by the Scope, and naming them here is cheaper
than discovering them mid-build.

**`lod_refs` — not used.** Octopus's `Model` carries level-of-detail
references. LOBSTER_SCOPE §1 is explicit: *"distance fog instead of LOD
popping"*, and §15's checklist treats LOD as a thing the design deliberately
does not have. Lobster reads `asset_ref` and ignores `lod_refs`. Content may
populate it for another consumer; this one will not look.

**`rig_ref` — not used yet, and that is a staging decision.** A model bound to a
rig is skinning, and skinning is geometry rather than animation policy, so it is
legitimately Lobster's. But it is a large surface — vertex weights, bone
matrices per frame, a mesh format that carries both — and entities are the only
things that would use it. Static props and items are the whole of the measured
gap today. **Staged out, not refused.**

---

## 6. Budget

`Model` is `accountable=True` in Octopus and carries `cost_estimate`, which is
Octopus's own budget hook and should be honoured rather than re-derived.

On Lobster's side the ceiling is per cell (L6) and must be **derived, not
chosen**, the way D20, D36 and D38 derived theirs: from a declared slice and a
measured per-unit cost. The unit here is meshed bytes per placement, and the
measurement cannot be taken until something is meshed — so the number is the
*last* step of the build, not the first.

---

## 7. Build order

1. **The manifest and the lint.** `models:` entries, `model_ref` → file
   resolution, and errors for both directions of a missing link. No runtime
   behaviour, testable immediately.
2. **The library artifact.** Mesh each distinct model once through the existing
   `StructureMesher`; write `models.lobster_lib`; a reader with the same
   provenance discipline as `read_bundle`.
3. **Residency.** Reference-counted load/release through `GpuResidency`, with
   `drift()` extended to cover models.
4. **Drawing, software first.** The software rasteriser gets real geometry for
   props and items — which is where the pixel oracle lives, and where a wrong
   mesh is visible rather than merely different.
5. **Drawing, GPU, with instancing.** Draw-call assertions against
   `RecordingContext` first: one upload per model, one instanced draw per
   model per cell, no per-placement buffer.
6. **The budget**, derived from what the previous steps measured.

---

## 8. Non-goals

* **No skinning** (§5), no animation, no blend trees — L8, and staged.
* **No LOD** (§5).
* **No textures, no atlases, no UVs** (§1).
* **No second authoring format.**
* **No runtime asset loading.** Models arrive in a build artifact, like
  everything else geometric. A runtime that reads content files is a runtime
  that can fail to find one on a player's machine.
* **No model editing**, at build time or otherwise. Runtime terrain editing is
  a §0 non-goal and this is the same argument.
