# Render scope — the GPU backend

**Status: scoped, not built.** This is the document D22 has owed since it named
ModernGL and wrote none of it. It exists because the seam is small and the
*decisions around* it are not.

> **A renderer sized for the actual target.** Baked per-cell lighting, small
> texture atlases or vertex-colored voxels, cell-boundary visibility, distance
> fog instead of LOD popping.
> — LOBSTER_SCOPE §1

Everything below serves that sentence. Where this document and
`LOBSTER_SCOPE.md` disagree, the Scope wins.

---

## 0. Why this was scoped rather than written

The interface is three methods and `SoftwareBackend` is 39 lines. On volume
alone it would have gone in dry. Two things said otherwise.

**There was no rule for what "correct" means**, and every other seam in this
project got one *before* the second implementation existed — D28 declared a
tolerance and named an authority, which is why the NumPy and C kernels dropped
in cleanly and why the harness caught D41. Rendering needed the equivalent
decision, and it turned out the right answer was to **delete the question**
(§2), which is not something you discover halfway through a shader.

**And scoping found a bug.** `build_draw_list` culls items in;
`render_draw_list` has branches for terrain, structures, entities and props and
**silently drops items**. D37 claimed items sit "exactly where props have always
been" — props get a capsule impostor, items get nothing. Written dry, the GPU
backend would have faithfully reproduced that gap and "both backends agree"
would have passed with neither drawing anything.

---

## 1. The target: toasters, phones, and a 2080 Ti

Three classes of machine, and the point is that **all three are acceptable
outcomes**, not that one is a degraded version of another.

| class | path | expectation |
|---|---|---|
| a real GPU | `moderngl` on hardware GL | the player-facing target — 60 FPS on a 128 m cell with destructible geometry, and the battery-efficient path on a phone |
| no GPU, Mesa present | `moderngl` on llvmpipe | development and many tests. Not player-facing |
| nothing at all | `software` | correct pixels for a screenshot, a debug overlay, a CI oracle |

That is D29's existing three-tier chain, unchanged. This document adds an
implementation behind the top two rungs; it does not renumber them.

### The feature baseline

**Stay inside a GL ES 3.0-compatible subset.** No geometry shaders, no compute,
no desktop-only texture formats, and instancing only where ES 3.0 has it.

The reason is portability of the *approach*, not of this code: ModernGL is
desktop GL 3.3+, and Python-plus-ModernGL is not an Android deployment. Writing
the draw model inside the ES subset means a future phone renderer reuses the
shaders and the buffer layout rather than starting over — and the subset costs
nothing here, because a voxel shell with flat shading and distance fog needs
none of what it gives up.

**Open question for the project owner.** If "phones" means something stronger
than that — an actual handset target — the language and runtime decision (D26)
reopens, and this baseline is not sufficient.

---

## 2. Pixel agreement: deliberately no rule

**Decided by the project owner: deleted.**

> *"We don't CARE if the pixels aren't identical between them when the two are
> screenshotted. If one has no GPU and one does, but the users are okay with
> both, then no need to compare/validate them between one another."*

This is the right call and worth recording why, because it inverts what D28 did
for the arithmetic seam.

The accelerator kernels answer a **question with one right answer** — which
entities are within a radius — so two implementations disagreeing is a defect,
and a declared tolerance makes "disagree" precise. Two rasterisers producing
different pixels is **not** a defect: edge fill rules, depth precision and
interpolation order all differ legitimately, and no tolerance separates
"different because correct" from "different because broken". A pixel test would
have been either permanently red or permanently meaningless.

So there is no pixel oracle, no SSIM, no per-channel epsilon, and the software
backend is **not** the reference implementation for rendering the way
`conformance.AUTHORITY` is for geometry. It is one acceptable output among
three.

**What replaces it — §5.**

---

## 3. What a backend returns

**Different types, on purpose, and no read-back per frame.**

| backend | returns |
|---|---|
| `software` | `Framebuffer` — `bytearray` colour + `list` depth |
| `moderngl` | a texture/framebuffer handle that stays on the GPU |

Reading a GPU framebuffer back to host memory every frame is a stall, and it
would be paid to satisfy a comparison §2 just deleted. The consumer handles
both.

### The consequence that needs naming

`write_png` and every pixel-asserting test read `Framebuffer.pixel(x, y)`. Under
this decision those work on the software path and **not** on the GPU path unless
something explicitly asks for the bytes.

So the seam gains one optional operation:

```python
frame = backend.render(draw_list, cells, settings)
frame.read_pixels()     # explicit, and never called by a frame loop
```

* `software` returns itself, or its buffer, at no cost.
* `moderngl` does a real read-back — a **screenshot**, which is not per-frame,
  which is why this is acceptable and why it must never be on the draw path.
* A backend that cannot read back raises, naming itself. Silence here would be
  the failure D29 exists to prevent.

CI keeps asserting pixels on the software path, which is where the pixel oracle
lives and is the only place it was ever meaningful.

---

## 4. Buffer lifetime — the real design work

`upload_cell` and `release_cell` exist on `RenderBackend` and **nothing calls
them.** Static geometry should reach the GPU once per residency, not once per
frame; that is the whole reason those methods are on a GPU-shaped interface.

### The obvious wiring is wrong

Calling `backend.upload_cell(cell)` from `CellManager.load` means `cell.py`
imports a render backend. `cell.py` is renderer-free today, and residency is
geometry, not presentation. That coupling is Lobster's own L8 mistake at a
different boundary — the same shape as Lobster deciding what to animate.

### Recommended: the backend subscribes to the events that already exist

`on_enter_cell` and `on_exit_cell` are contract Events, fired by
`CellManager.load` and `.unload`, and CONTRACT §1 already says what they mean:

> loading a neighbouring cell for residency is not the player entering a scene

That is exactly buffer lifetime. So:

```python
backend = select_backend()
backend.attach(bus, manager)      # subscribes; no new coupling anywhere
```

* `on_enter_cell` → `upload_cell`
* `on_exit_cell` → `release_cell`
* The manager learns nothing about rendering, and the events are already
  ordered correctly, including across the fast-travel release-then-load path
  (D32).

**The alternative**, if the event route proves awkward: the caller that owns
both the manager and the backend calls them explicitly. It works and it puts the
knowledge in the one place that legitimately has both — but it is easy to forget
on one path and leak GPU memory silently, which is the argument for the bus.

### What must be settled while building

1. **What actually goes in a buffer.** Terrain mesh and structure meshes are
   static per residency. Break-state changes structure geometry *at runtime*
   (`geometry_version` moves), so a damaged structure needs a re-upload of the
   touched chunk, not the cell. That is the one genuinely dynamic case and it
   has a version number to key on already.
2. **Entities and props are impostors**, rebuilt per frame from poses. They do
   not belong in a residency buffer.
3. **A budget.** GPU memory is memory (L6). `MAX_RESIDENT_CELLS` bounds the
   count; the per-cell byte figure should be derived, not chosen, the way D20
   and D36 derived theirs.
4. **Release must be exact.** A cell unloaded without a matching release is a
   leak, and D43 is a fresh reminder that leaks pass every test that only checks
   answers. Assert upload/release pairing directly.

---

## 5. How correctness is established

With §2 deleted, two mechanisms — and neither needs a GPU in CI.

**Mocked-API draw-call assertions.** A fake ModernGL context records what the
backend did: buffers created, shaders compiled, uniforms set, draw calls issued,
in order. Then assert the things that are actually true regardless of pixels —
one upload per cell residency and not per frame; every uploaded buffer released
on unload; the draw list's items each producing a draw; culled items producing
none; a damaged structure re-uploading only the touched chunk. This runs
anywhere, including the pure-Python CI job.

**Visual inspection.** A human looks at it. Recorded as a real method rather than
an embarrassment: the software rasteriser's two genuine bugs — inverted terrain
normals and missing near-plane clipping — were both found by looking at a PNG
after every geometry test had passed (D22).

**And the existing suite, unchanged.** The GPU backend must not alter culling,
draw-list construction or any geometry, all of which are already covered.

---

## 6. Build order

1. **Fix the item-rendering gap** in the software backend, so the GPU backend is
   written against a complete draw list rather than reproducing a hole.
   *(Project owner: "Fix the software backend's item bug first.")*
2. **The mock context and its assertions**, before the backend — same discipline
   as D28 building the harness before the kernels, and for the same reason: it
   cannot become a third named-but-unwritten thing.
3. **`upload_cell`/`release_cell` wiring** via the event bus, with pairing
   asserted.
4. **The backend itself**: shaders, buffer packing, draw loop.
5. **`read_pixels`**, and a screenshot compared by eye.
6. **CI**: the accelerated matrix gains an llvmpipe job on Linux — Mesa is one
   `apt` line, and it exercises the real GL path with no GPU present.

---

## 7. Non-goals

Stated so they are refused once rather than argued repeatedly.

* **No asset pipeline.** `model_ref` still resolves to nothing. Items and props
  stay impostors. This is the largest gap between "Lobster draws" and what a
  player sees, and it is a separate decision nobody has asked for.
* **No shadow maps, no post-processing, no PBR.** Baked per-cell lighting and
  distance fog are what §1 asks for.
* **No LOD.** The Scope is explicit: *distance fog instead of LOD popping*.
* **No window, no input, no frame loop.** Lobster renders; it is not an
  application. A viewer belongs to whoever owns the game loop.
* **The software backend is not deprecated.** It is the zero-dependency path and
  CI's pixel oracle, and D22's demotion of it to "reference and fallback" stands
  without meaning "temporary".
