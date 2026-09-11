# Asset scope — making `model_ref` mean something

**Status: built, and the budget has been re-derived once since.** All six
steps of §7 are done, and §6's ceiling moved from 312 visible placements a
frame to 952 when the accelerator seam gained a third kernel (D53) - which is
the mechanism §6 named: *"raising it means making a placement cheaper again,
not editing the number."* The kind registry and its lint
(step 1); `models.lobster_lib`, both kinds meshed once into the vertex format
the GL backend already draws (step 2, D47); reference-counted model residency
across resident cells, props *and* items, with `drift()` extended to catch a
leak (steps 3 and 5, D48/D50); real geometry on the software rasteriser (step 4,
D49); instanced drawing on the GPU, one draw per distinct model per frame (step
5, D50); and a **derived** budget — a declared slice over a measured unit, taken
last, exactly as §6 insisted (step 6, D51).

`model_ref` means something now. What it still does not mean is a skinned
character or a second authoring format — §5 and §8 are unchanged.

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

## 1. Two model kinds, declared rather than assumed

**The project owner said no to "every model is a voxel file", and asked for
exceptions.** That answer changes the architecture more than it changes the
formats, and the architecture change is the valuable half.

A scope that says *a model is `.vox`* makes every exception an `if` in the
loader. A scope that says *a model declares its kind* makes exceptions
ordinary. This project already does that twice — render backends (D29) and
accelerator kernels (D40) — and both times the shape is the same: **a declared
set, a chooser that says which it picked, and every implementation held to the
same tests.**

So `MODEL_KINDS` is a closed, frozen set, exactly as `ZONE_SHAPE_PRIMITIVES` is
frozen at three (§4): adding a fourth later is a contract change with a
DECISIONS entry, not a quiet extension.

| kind | geometry from | when |
|---|---|---|
| `voxel` | a MagicaVoxel `.vox` file, via `Model.asset_ref` | the default: anything authored |
| `primitive` | a shape and dimensions declared in the record itself | a crate is a crate; blockout; placeholders |

### `voxel` — the default, and the Scope's own choice

§4.5 names MagicaVoxel `.vox` as *the* authoring format, and §1 offers "small
texture atlases **or** vertex-colored voxels" — a `.vox` carries a 256-entry
palette, so the `or` is already answered. Each of these removes a subsystem:

* **No texture pipeline.** No atlas packer, no UVs, no samplers, no mipmaps, no
  texture memory budget. The GL shader already takes a per-vertex tint.
* **The same mesher.** `StructureMesher` already turns voxels into greedy-meshed
  quads with palette materials. A second mesher would be the thing L2 forbids
  for terrain and structures, without even L2's excuse.

### `primitive` — geometry with no asset at all

A box, a cylinder or a quad, declared inline with dimensions and a palette
index. No file, no importer, no round trip through an art tool.

```json
{"id": "model-crate", "type": "Model",
 "primitive": {"shape": "box", "size": [0.8, 0.8, 0.8], "material": 6}}
```

This earns its place for three reasons and not because it is easy:

1. **It removes the need for most exceptions rather than being one.** A crate,
   a plank, a doorway marker, a debug volume — things that are genuinely a box
   do not become more correct by being drawn in MagicaVoxel first.
2. **It has no build-time dependency.** A primitive resolves with no `vox_dir`,
   no file, and no art pipeline, so content and mods can ship a placeable
   object with nothing but a record. That is the case D33 wanted for
   pre-placed items and could not have.
3. **It meshes to the same vertex format.** A box is twelve triangles with
   normals and a palette tint — the buffer the GL backend already draws and the
   quads the software rasteriser already fills. Nothing downstream learns a
   second representation.

The shape set is frozen at three, for the reason §4 freezes zone shapes: three
primitives cover the cases, and a fourth is a decision rather than a
convenience.

### `Model` declares exactly one of them

Lobster adds `Model.primitive` through `schema_extensions.new_fields`, the same
mechanism that added `Item.world_transform` (D33), `Location.exterior_grid` and
`Zone.shape`. `asset_ref` is Octopus's and already exists.

> **Invariant — a `Model` has `asset_ref` **or** `primitive`, never both and
> never neither.**

Deliberately the same shape as D33's co-null rule, and for the same reason: two
sources of geometry for one model is a question about which wins, and no
sources is a model that silently does not appear. Both are build errors.

### What is *not* a kind, and will not become one

Sprites and conventional meshes were both considered and are **not** in the set.

* **Sprites.** `Item.sprite_ref` and Octopus's `Sprite` record exist, and a
  textured billboard would slot into the impostor path almost directly. It
  needs a texture path — PNG *decode*, which this project does not have — and
  §1's "vertex-colored voxels" branch was taken instead. **Reconsidered and
  declined** (D56): the decoder was never the real objection. A billboard turns
  to face the viewer, and the objects that raised the question — a candlestick,
  a chalice on a table — are exactly the ones a player walks around at arm's
  length, where that swivel is most visible. Right for distant foliage, wrong
  for tableware.
* **Conventional meshes** (glTF/OBJ). A second format contract with content
  authors, a material model, and the door skinned characters come through. See
  §5: that is a new scope.

**Neither is a primitive's slippery slope.** A primitive is a *fewer*-assets
kind, not a richer one — it adds no format, no importer and no file. If someone
proposes `"shape": "mesh"` with a path in it, that is the mesh importer wearing
a primitive's clothes, and this paragraph is the objection.

**Answered by the project owner (D56): voxels are fine.** Every *authored* prop
and item is a voxel model, with primitives for the ones that are geometrically
trivial, and no sprite path. **Conditional on a scale this scope has not set.**
The question was asked about a candlestick, and at the structure voxel size a
candlestick is a 1x4x1 stack — one block wide, 25 cm thick, a fencepost. At 1 cm
it is a 9x32x9 grid that meshes to 214 triangles, 22 KB, and looks like a
candlestick. So the answer holds *at a per-model voxel scale*, which is step 2a
of §7 and is not built. Reading this paragraph as approval of 0.25 m for props
would invert it.

Characters are **not** covered by that answer. If they or their weapons need
conventional or skinned meshes, that remains a separate scope — see §5.

**And if that answer changes later, it is a new scope — not an addition to this
one.** Conventional or skinned meshes for entities would bring a second
authoring format, vertex weights, per-frame bone matrices and a material model,
and every one of those arrives most cheaply disguised as *"just one more model
type"* in a pipeline that already loads models. That is the exact shape of creep
L8 exists to catch, one layer below where L8 usually catches it. Refusing it
here in writing is cheaper than refusing it in a review.

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

### Reference counts get the same treatment cell pairing already has

A refcount is precisely the class of bug D43 recorded: **every answer stays
correct while the memory grows.** The leaked reference in the C kernel passed
2,400 differential cases, because those compare outputs — and a model that is
never released draws perfectly.

So `drift()` extends to models, and the invariants are asserted directly rather
than inferred from frames looking right:

* a model referenced by *n* resident cells is uploaded **once**, not *n* times;
* releasing one of those cells does **not** release the model;
* releasing the last one **does**;
* a count never goes negative, and a release for a model never uploaded is
  counted rather than obeyed — the same shape as `unknown_releases` today;
* after any sequence of loads and unloads, the set of live models equals the
  set of models the resident cells actually reference.

That last one is the model-level analogue of the existing
`{"leaked": [...], "missing": [...]}` check, and it is what a test asserts in
one line after a randomised walk.

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

### The lint is bidirectional, with two different severities

Three things can be wrong, and they are not equally wrong:

| code | fires on | severity |
|---|---|---|
| `model_has_no_geometry` | a `Model` with neither `asset_ref` nor `primitive` | **error** |
| `model_has_two_geometries` | a `Model` with both | **error** |
| `unknown_primitive_shape` | a shape outside the frozen set | **error** |
| `model_ref_unresolved` | an `asset_ref` with no manifest entry | **error** |
| `model_file_missing` | a manifest entry whose `.vox` is not there | **error** |
| `model_asset_unused` | a `.vox` in `vox_dir` that no entry names | **warning** |

**A primitive model must not trip the file checks.** It has no `asset_ref` by
construction, so the resolution lint has to read the *kind* before it looks for
a file — otherwise the cheapest kind fails the strictest check, which is the
sort of thing that gets a whole feature written off as broken.

The first two are the same shape as `item_transform_without_location` (D33): an
unresolvable model at runtime is a thing that silently does not appear, and this
project's posture is that such things fail at build time instead.

The third is deliberately **not** an error. An unused `.vox` is dead weight, not
a broken build, and an art directory mid-iteration is full of them. Failing
somebody's build for a file they have not wired up yet trains them to ignore the
linter, which costs more than the dead file does. It is reported so it stays
attributable, and `errors()` leaves it out.

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
measurement cannot be taken until something is meshed.

> **No placeholder ceiling. Not even a temporary one.**

This is a hard constraint on the build order, not a preference. A number
invented before there is anything to measure survives: it acquires tests, it
gets quoted in a document, and by the time real numbers exist it is load-bearing
and nobody remembers it was a guess. D36 shipped 26 items per cell and D38
raised it to 173 — both derived, and the *second* was only possible because the
first was a real division rather than a round number somebody liked.

So the budget step comes strictly after meshing and residency are real, and
until then **there is no ceiling at all**. An unbounded model library is a
visible, honest gap for the few commits it exists; a placeholder would be an
invisible dishonest one.

---

## 7. Build order

1. **The kind registry and the lint.** `MODEL_KINDS`, `Model.primitive` in the
   package, the one-of invariant, `models:` manifest entries, and every code in
   §3. No runtime behaviour and no meshing — testable immediately, and it is
   what makes the second kind ordinary instead of a special case.
2a. **A model's voxel scale.** Props and items do not inherit
   `VOXEL_SIZE_M`. `mesh_voxel_file` already takes `voxel_size`; nothing
   passes one, so every voxel model bakes at a structure's 0.25 m. Per
   model rather than one new constant, because a candlestick and a wardrobe
   want different grids and allowing both costs nothing (D56). Sits with
   step 2 because it is the call site that changes.

2. **The library artifact, both kinds.** Mesh each distinct model once —
   voxels through the existing `StructureMesher`, primitives through a
   dozen-line generator — into the same vertex format; write
   `models.lobster_lib`; a reader with the same provenance discipline as
   `read_bundle`. **Primitives first**, because they need no file and so prove
   the library end to end before the importer is involved.
3. **Residency.** Reference-counted load/release through `GpuResidency`, with
   `drift()` extended to cover models.
4. **Drawing, software first — and this step is not skippable.** The software
   rasteriser gets real geometry for props and items before the GPU does.

   It is the only place a wrong mesh is **visibly wrong** rather than merely
   *different from the GPU*. RENDER_SCOPE §2 deleted pixel agreement between
   the backends, which was right and which also means the GPU path has no
   oracle: a mesh drawn inside-out, at the wrong scale, or with inverted
   normals would render, differ from the software path, and be
   indistinguishable from the legitimate differences that decision permits. On
   the software path it is simply a picture that is wrong.

   Doing the GPU first and the software path "later" would mean the first
   correct-looking frame proves nothing.
5. **Drawing, GPU, with instancing.** Draw-call assertions against
   `RecordingContext` first: one upload per model, one instanced draw per
   model per cell, no per-placement buffer.
6. **The budget**, derived from what the previous steps measured.

---

## 8. Non-goals

* **No skinning** (§5), no animation, no blend trees — L8, and staged.
* **No LOD** (§5).
* **No textures, no atlases, no UVs** (§1).
* **No second authoring format.** `primitive` is not one: it declares geometry
  in a record rather than importing it from a file.
* **No sprites, for now.** Considered (§1); needs a PNG decoder this project
  does not have, and §1's other branch was taken. A real option later, not a
  gap.
* **No runtime asset loading.** Models arrive in a build artifact, like
  everything else geometric. A runtime that reads content files is a runtime
  that can fail to find one on a player's machine.
* **No model editing**, at build time or otherwise. Runtime terrain editing is
  a §0 non-goal and this is the same argument.
