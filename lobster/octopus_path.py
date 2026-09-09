"""Locating Octopus.

Lobster reads Octopus; it never vendors or forks it. "Prefer calling the real
pure query functions over re-implementing them" - so `lce` has to be
importable, and when it is not, the failure must say exactly what to do rather
than surfacing as a bare ImportError three modules deep.

Search order:
  1. `LOBSTER_OCTOPUS_PATH` environment variable
  2. a sibling `Octopus/` checkout next to this repository
  3. whatever is already on `sys.path`
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

ENV_VAR = "LOBSTER_OCTOPUS_PATH"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def candidate_paths() -> List[str]:
    out: List[str] = []
    env = os.environ.get(ENV_VAR)
    if env:
        out.append(os.path.abspath(env))
    out.append(os.path.abspath(os.path.join(_REPO_ROOT, os.pardir, "Octopus")))
    return out


def ensure_lce_importable() -> Optional[str]:
    """Put Octopus on `sys.path` if it is not already importable.

    Returns the path that was added, or None if `lce` was already reachable.
    """
    try:
        import lce  # noqa: F401
        return None
    except ImportError:
        pass
    for path in candidate_paths():
        if os.path.isdir(os.path.join(path, "lce")):
            if path not in sys.path:
                # APPENDED, not prepended. Octopus has its own `tests` package,
                # and putting its root at the front of sys.path shadows the
                # host project's modules of the same name - which is a hard
                # failure to diagnose from the far end. Lobster is the guest
                # here; it goes last.
                sys.path.append(path)
            return path
    raise ImportError(
        "Lobster needs the Octopus engine (`lce`) on the import path and could "
        "not find it. Set {0} to your Octopus checkout, or place it beside this "
        "repository as ../Octopus. Looked in: {1}".format(
            ENV_VAR, ", ".join(candidate_paths())))
