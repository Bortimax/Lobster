"""Drawing (Scope 1, "a renderer sized for the actual target").

A software rasteriser and a PNG writer, both standard library. This is the
consumer that `TerrainMesh`, `StructureMesher`, `bone_matrices`, the material
palette and the baked lightmap were always inputs to, and until it existed
every one of them was an output nothing read.

**What this is:** an offline viewer. It resolves a camera and a draw list into
an image, on the CPU, in Python. It is how you look at a bake and confirm the
geometry is what you think it is, how a test asserts against pixels rather than
against numbers that only mean something to their author, and how a build
machine with no GPU produces a screenshot.

**What this is not:** the renderer a player runs. It does not open a window, run
a frame loop, or read input, and at cell scale in Python it will not hit any
frame rate worth the name.

That is what `backend.py` is for. Lobster may use the GPU when it is available
(DECISIONS.md D22): `select_backend()` prefers a GPU backend and falls back to
this one, saying why. Core Lobster keeps no hard graphics dependency, so every
test still runs on a machine with no GPU and no display - which is also what
makes this rasteriser worth keeping as CI's pixel oracle.

The split that matters: `lobster.camera` and `lobster.visibility` are the
geometry, and every backend consumes them. This package is one consumer, not the
contract.
"""

from .backend import (BackendError, BackendInfo, RenderBackend,
                      SelectionReport, probe, probe_with, select_backend,
                      selection_report)
from .png import write_png
from .recording import RecordingBackend, RecordingContext
from .residency import GpuResidency, ResidencyError
from .raster import (Framebuffer, RenderSettings, render_cell,
                     render_draw_list, render_resident)
from .software_backend import SoftwareBackend

__all__ = ["BackendError", "BackendInfo", "Framebuffer", "RenderBackend",
           "RenderSettings", "SelectionReport", "SoftwareBackend", "probe",
           "probe_with", "render_cell", "render_draw_list", "render_resident",
           "select_backend", "selection_report", "write_png",
           "GpuResidency", "RecordingBackend", "RecordingContext",
           "ResidencyError"]
