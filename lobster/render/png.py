"""Minimal PNG writer. Standard library only (`zlib`, `struct`).

PNG rather than PPM because the point of an offline viewer is that somebody can
open the file, and every image viewer, browser and pull request already reads
PNG. Forty lines of stdlib is a better trade than a dependency for a debug
artifact (DECISIONS.md D6).

Truecolour, 8 bits per channel, no interlacing, filter type 0. Nothing clever:
a viewer's output should be boring and correct.
"""

from __future__ import annotations

import struct
import zlib
from typing import Sequence


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode_png(width: int, height: int, pixels: Sequence[int]) -> bytes:
    """Encode RGB bytes (row-major, 3 per pixel) as a PNG file.

    `pixels` must hold exactly `width * height * 3` values in 0..255.
    """
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(
            "expected {0} colour bytes for a {1}x{2} image, got {3}".format(
                expected, width, height, len(pixels)))

    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)                              # filter type 0: none
        raw.extend(pixels[row * stride:(row + 1) * stride])

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def write_png(path: str, width: int, height: int,
              pixels: Sequence[int]) -> str:
    """Write an RGB image. Build/tooling only - the runtime never writes files."""
    import os
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as f:
        f.write(encode_png(width, height, pixels))
    return path
