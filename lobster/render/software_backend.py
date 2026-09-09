"""The software backend: the reference rasteriser behind the backend seam.

Always available - it is standard library and needs no device, no display and
no driver, which is exactly what makes it the right fallback and the right
thing for CI to assert pixels against.

It is not the renderer a player runs. `raster.py` says so at length and
DECISIONS.md D22 records why: hand-rolled scanline rasterisation in Python is
not the industry's solved answer to drawing 3D, and treating it as one was the
L1 inversion this seam exists to correct.

`upload_cell` and `release_cell` are no-ops here. They are on the interface for
the GPU backend's sake - static geometry uploaded once per residency rather
than per frame - and a CPU rasteriser that walks the mesh every frame has
nothing to prepare.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .backend import BackendInfo, RenderBackend


class SoftwareBackend(RenderBackend):
    """CPU rasteriser. Reference and fallback."""

    name = "software"

    @classmethod
    def available(cls) -> BackendInfo:
        return BackendInfo(
            name=cls.name, available=True,
            detail="standard library only; no device, display or driver needed")

    def render(self, draw_list: Any, cells_by_id: Dict[str, Any],
               settings: Any) -> Any:
        from .raster import render_draw_list
        return render_draw_list(draw_list, cells_by_id, settings=settings)
