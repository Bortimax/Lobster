"""Build the native seam kernels.

    python lobster/accel/native/setup.py build_ext --inplace

Deliberately plain setuptools with no build dependency beyond a C compiler: CI
needs no cargo, no maturin, no Cython. The binary is **not** committed - only
the `.c` - so every platform builds and differentially tests its own (D42).

The extension is named by its **package path** and the script anchors itself to
the repo root, so `--inplace` lands the binary in `lobster/accel/native/`
whatever directory the command was run from. Naming it bare put a stray `.pyd`
wherever the shell happened to be, and the CI check that the kernel built would
then have been passing by accident of `cwd`.
"""

import os
from setuptools import setup, Extension

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir))

# `build_ext --inplace` resolves the extension's package path against the
# *current* directory, so running this from anywhere but the repo root put the
# binary somewhere the loader would never look - and failed with a compiler
# error rather than saying so. Anchoring here makes the command work from any
# directory, which is what a developer and a CI runner both expect.
os.chdir(ROOT)
SOURCE = os.path.join("lobster", "accel", "native", "lobster_accel.c")

setup(
    name="lobster-accel",
    version="0.1.0",
    package_dir={"": "."},
    ext_modules=[Extension("lobster.accel.native.lobster_accel", [SOURCE])],
)
