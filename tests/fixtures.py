"""Synthetic world for the section 14 acceptance suite.

Deliberately small and deliberately *content*: three cells, one keep with a
gatehouse, a handful of characters. Nothing here is a Lobster default - every
budget, spawn transform and structure placement is authored, because L7 says
bloat in a finished build must be traceable to content, and a test fixture that
leans on shell defaults cannot demonstrate that.

The Octopus side is built the way a real game would build it: the day-one
catalog, plus Lobster's schema-extension packages, plus a content package of
ordinary CREATE operations. There is no test-only schema path.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lobster.build.bundle_writer import write_bundle
from lobster.bundle import CellBundle, PropPlacement
from lobster.constants import BUNDLE_SUFFIX, EXTERIOR_CELL_SIZE_M
from lobster.geometry import Transform
from lobster.navmesh import LoadBearingTable, NavPoly, Navmesh
from lobster.octopus_path import ensure_lce_importable
from lobster.structures import StructureVoxelData
from lobster.terrain import Terrain, TerrainCollider, TerrainMesh

ensure_lce_importable()

from lce.catalog import build_day_one_schema  # noqa: E402
from lce.package import build_stack, load_package  # noqa: E402
from lce.save import new_save  # noqa: E402
from lce.session import GameSession  # noqa: E402

PACKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "packages")

VILLAGE = "cell-village"
FIELD = "cell-field"
KEEP = "cell-keep"
GATEHOUSE = "keep-gatehouse"

DAY = 1440


def C(rid: str, rtype: str, **fields: Any) -> Dict[str, Any]:
    rec = {"id": rid, "type": rtype}
    rec.update(fields)
    return {"op": "CREATE", "record": rec}


def spawn(x: float, y: float, z: float) -> Dict[str, Any]:
    return Transform(position=(x, y, z)).to_dict()


def content_ops(*, gatehouse_location: str = VILLAGE) -> List[Dict[str, Any]]:
    """The world as ordinary Octopus operations."""
    return [
        C("cal", "CalendarDefinition", day_length_ticks=DAY,
          day_segments=[{"name": "Morning", "start_tick": 0, "end_tick": 720},
                        {"name": "Evening", "start_tick": 720, "end_tick": DAY}],
          weekday_names=["Monday", "Tuesday"]),
        C("clock", "GlobalClock"),
        C("settings", "GameSettings", player_character_ref="player"),

        C(VILLAGE, "Location", display_name="Village", tags=["exterior"],
          exterior_grid=[0, 0],
          default_spawn_transform=spawn(64, 0, 64),
          lobster_budget={"max_active_skeletons": 8},
          sound_sources=[{"position": [10, 0, 10], "sound_id": "snd-well",
                          "radius": 12}],
          connections=[
              {"target_location_id": FIELD, "label": "north path",
               "spawn_transform": spawn(64, 0, 4)},
              {"target_location_id": KEEP, "label": "keep door",
               "spawn_transform": spawn(8, 0, 8)},
          ]),
        C(FIELD, "Location", display_name="Field", tags=["exterior"],
          exterior_grid=[0, 1],
          default_spawn_transform=spawn(64, 0, 64),
          connections=[{"target_location_id": VILLAGE, "label": "south path",
                        "spawn_transform": spawn(64, 0, 124)}]),
        C(KEEP, "Location", display_name="Keep interior",
          default_spawn_transform=spawn(4, 0, 4),
          connections=[{"target_location_id": VILLAGE, "label": "keep door",
                        "spawn_transform": spawn(10, 0, 10)}]),

        C("zone-village", "Zone", display_name="Village zone",
          location_refs=[VILLAGE, KEEP], shape_location_ref=VILLAGE,
          shape={"kind": "box", "center": [64, 2, 64], "size": [128, 8, 128]}),

        C("race-human", "Race", display_name="Human"),
        C("player", "Character", display_name="Player", race_ref="race-human",
          home_location_ref=VILLAGE),
        C("npc-ada", "Character", display_name="Ada", race_ref="race-human",
          home_location_ref=VILLAGE, limb_state={"left_arm": "intact"}),
        C("npc-bren", "Character", display_name="Bren", race_ref="race-human",
          home_location_ref=FIELD),
    ]


def gatehouse_ops(*, location_id: str = VILLAGE,
                  destroyed_chunks: Sequence[int] = ()) -> List[Dict[str, Any]]:
    """The gatehouse's `StructureState` - the one save-integrated record.

    Shipped as its own package so the section 14 round-trip test can remove and
    reinstall exactly it, which is what a player uninstalling a mod actually
    does. `destroyed_chunks` is authored empty here; a mod may ship a
    pre-ruined keep by authoring it non-empty, which is the case
    UNION_TOMBSTONED exists for.
    """
    return [C(GATEHOUSE, "StructureState", display_name="Gatehouse",
              location_id=location_id,
              destroyed_chunks=sorted(int(i) for i in destroyed_chunks))]


def package_dict(package_id: str, ops: Sequence[Dict[str, Any]], *,
                 version: str = "1.0.0",
                 dependencies: Optional[List[Dict[str, str]]] = None
                 ) -> Dict[str, Any]:
    return {"format": "lce-package", "package_id": package_id,
            "version": version, "schema_compat": {"min": 1, "max": 1},
            "dependencies": dependencies or [],
            "operations": list(ops)}


def build_session(extra_packages: Sequence[Dict[str, Any]] = (), *,
                  include_world: bool = True,
                  include_gatehouse: bool = True,
                  gatehouse_destroyed: Sequence[int] = (),
                  save_data: Optional[Dict[str, Any]] = None) -> GameSession:
    """A GameSession over Lobster's schema extensions plus the fixture world.

    `include_gatehouse=False` is "the mod that shipped this structure has been
    uninstalled" - the exact situation the break-state round trip has to
    survive.
    """
    from lce.package import package_from_dict
    schema = build_day_one_schema()
    packages = [load_package(os.path.join(PACKAGE_DIR, "lobster_geometry.json")),
                load_package(os.path.join(PACKAGE_DIR, "lobster_limb_state.json"))]
    if include_world:
        packages.append(package_from_dict(package_dict("world.base", content_ops())))
    if include_gatehouse:
        packages.append(package_from_dict(package_dict(
            "mod.keep", gatehouse_ops(destroyed_chunks=gatehouse_destroyed))))
    for raw in extra_packages:
        packages.append(package_from_dict(raw))
    result = build_stack(packages, schema)
    data = save_data if save_data is not None else new_save(
        "test-save", seed=1, content_layer_ids=result.stack.layer_ids())
    return GameSession.start(schema, result.stack, data)


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

def flat_terrain(cell_id: str, *, height: float = 0.0,
                 resolution: int = 5) -> Terrain:
    mesh = TerrainMesh(cell_id=cell_id,
                       vertices=(0.0, height, 0.0,
                                 EXTERIOR_CELL_SIZE_M, height, 0.0,
                                 EXTERIOR_CELL_SIZE_M, height, EXTERIOR_CELL_SIZE_M,
                                 0.0, height, EXTERIOR_CELL_SIZE_M),
                       indices=(0, 1, 2, 0, 2, 3))
    collider = TerrainCollider(cell_id=cell_id, resolution=resolution,
                               size_m=EXTERIOR_CELL_SIZE_M,
                               heights=tuple([height] * (resolution ** 2)))
    return Terrain(cell_id=cell_id, mesh=mesh, collider=collider)


def corridor_navmesh(cell_id: str, *, count: int = 6, width: float = 4.0,
                     length: float = 4.0,
                     gate_index: int = 3) -> Navmesh:
    """A single-file corridor of `count` polys laid along +Z.

    Used by three tests: the navmesh/connection-graph agreement test severs it
    at `gate_index`, the leader-leash test queues a war party through it, and
    the async-patch test watches a poly go dirty.
    """
    polys = []
    for i in range(count):
        z0, z1 = i * length, (i + 1) * length
        neighbours = tuple(n for n in (i - 1, i + 1) if 0 <= n < count)
        polys.append(NavPoly(poly_id=i,
                             points=((0.0, z0), (width, z0), (width, z1), (0.0, z1)),
                             y=0.0, neighbours=neighbours))
    return Navmesh(cell_id, polys)


def solid_structure(structure_id: str, *, grid_size: int = 16,
                    origin: Optional[Transform] = None,
                    chunk_size: int = 8) -> StructureVoxelData:
    return StructureVoxelData(
        structure_id=structure_id, grid_size=grid_size,
        material_ids=bytes([1]) * (grid_size ** 3),
        origin=origin or Transform(position=(0.0, 0.0, 0.0)),
        chunk_size=chunk_size)


class BundleWorkspace:
    """A temp directory of `.lobster_cell` bundles, cleaned up on close."""

    def __init__(self) -> None:
        self.path = tempfile.mkdtemp(prefix="lobster-cells-")

    def write(self, bundle: CellBundle) -> str:
        return write_bundle(bundle,
                            os.path.join(self.path, bundle.cell_id + BUNDLE_SUFFIX))

    def write_library(self, library: Any) -> str:
        """Put a `models.lobster_lib` beside the cells, where the real build
        puts one."""
        from lobster.build.library_writer import write_library
        from lobster.constants import LIBRARY_FILENAME
        return write_library(library, os.path.join(self.path,
                                                   LIBRARY_FILENAME))

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "BundleWorkspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def village_bundle(*, gate_polys: Sequence[int] = (3,),
                   gate_chunk: int = 0,
                   structure_origin: Optional[Transform] = None,
                   grid_size: int = 16) -> CellBundle:
    """The village cell: flat terrain, a corridor navmesh, one gatehouse.

    The gatehouse's chunk `gate_chunk` is the one the build step infers as
    load-bearing over `gate_polys` - here stated directly, because the
    inference itself is tested against real geometry in
    `test_navmesh_inference`, and this fixture is about what happens *after* it
    has run.
    """
    structure = solid_structure(GATEHOUSE, grid_size=grid_size,
                                origin=structure_origin
                                or Transform(position=(0.0, 0.0, 12.0)))
    return CellBundle(
        cell_id=VILLAGE,
        terrain=flat_terrain(VILLAGE),
        navmesh=corridor_navmesh(VILLAGE),
        structures=(structure,),
        load_bearing={GATEHOUSE: LoadBearingTable(
            structure_id=GATEHOUSE,
            inferred={gate_chunk: tuple(gate_polys)})},
        provenance={"records": [VILLAGE, GATEHOUSE], "manifest_sha256": "fixture"})


def plain_bundle(cell_id: str,
                 props: Sequence[PropPlacement] = ()) -> CellBundle:
    return CellBundle(cell_id=cell_id, terrain=flat_terrain(cell_id),
                      navmesh=corridor_navmesh(cell_id), props=tuple(props),
                      provenance={"records": [cell_id]})


def prop(prop_id: str, model_ref: str,
         position: Tuple[float, float, float] = (4.0, 0.0, 4.0)
         ) -> PropPlacement:
    return PropPlacement(prop_id=prop_id, model_ref=model_ref,
                         transform=Transform(position=position))


#: one legal spec per frozen primitive shape, so a fixture can ask for a model
#: by name without every test restating the geometry.
PRIMITIVE_SPECS = {
    "model-crate": {"shape": "box", "size": [0.8, 0.8, 0.8], "material": 6},
    "model-barrel": {"shape": "cylinder", "radius": 0.35, "height": 0.9,
                     "material": 6},
    "model-sign": {"shape": "quad", "size": [0.6, 0.4], "material": 5},
}


def primitive_library(*model_refs: str) -> Any:
    """A `ModelLibrary` of real meshed primitives, keyed as `PRIMITIVE_SPECS`.

    Real geometry rather than stubs: residency counts references, and a
    zero-byte model is exactly the case `upload_model` skips.
    """
    from lobster.build.model_mesher import mesh_primitive
    from lobster.model_library import ModelLibrary
    refs = model_refs or tuple(PRIMITIVE_SPECS)
    return ModelLibrary(models={ref: mesh_primitive(ref, PRIMITIVE_SPECS[ref])
                                for ref in refs},
                        provenance={"records": sorted(refs)})


def standard_workspace() -> BundleWorkspace:
    ws = BundleWorkspace()
    ws.write(village_bundle())
    ws.write(plain_bundle(FIELD))
    ws.write(plain_bundle(KEEP))
    return ws


# ---------------------------------------------------------------------------
# The bridge scenario - a raised walkway whose deck IS the structure
# ---------------------------------------------------------------------------
#
# Used by the navmesh/connection-graph agreement test and by the sync/async
# race test. A flat cell cannot exercise either: if terrain supports every
# polygon, destroying a structure can never make one unwalkable, and the whole
# load-bearing path is untestable. So the walkway here is held up by the
# structure and nothing else.

BRIDGE = "keep-gatehouse"          # the same StructureState record
BRIDGE_DECK_Y = 4.0
BRIDGE_POLY_M = 2.0
BRIDGE_GRID = 48                   # 48 voxels * 0.25 m = 12 m cube
BRIDGE_SPAN = 6                    # 6 polys of 2 m = 12 m


def deck_structure(structure_id: str = BRIDGE) -> StructureVoxelData:
    """A 12 m cube that is solid below the deck and open above it.

    Solid-everywhere would put voxels in the walkway's headroom, and the
    recompute would - correctly - call every polygon blocked. A bridge you can
    actually walk on is the point of the fixture.
    """
    grid = BRIDGE_GRID
    deck_voxels = int(BRIDGE_DECK_Y / 0.25)          # 16
    materials = bytearray(grid ** 3)
    for z in range(grid):
        for y in range(deck_voxels):
            base = y * grid + z * grid * grid
            for x in range(grid):
                materials[base + x] = 1
    return StructureVoxelData(structure_id=structure_id, grid_size=grid,
                              material_ids=bytes(materials),
                              origin=Transform(position=(0.0, 0.0, 0.0)),
                              chunk_size=8)


def bridge_navmesh(cell_id: str = VILLAGE, *, span: int = BRIDGE_SPAN,
                   portal_target: str = FIELD) -> Navmesh:
    """A single-file walkway at deck height, with the far end a portal.

    Poly 0 is the reference (where an arrival stands); the last poly carries
    `connection_target`, which is what makes it a door in Scope 4's sense.
    """
    polys = []
    for i in range(span):
        z0, z1 = i * BRIDGE_POLY_M, (i + 1) * BRIDGE_POLY_M
        polys.append(NavPoly(
            poly_id=i,
            points=((0.0, z0), (BRIDGE_POLY_M, z0),
                    (BRIDGE_POLY_M, z1), (0.0, z1)),
            y=BRIDGE_DECK_Y,
            neighbours=tuple(n for n in (i - 1, i + 1) if 0 <= n < span),
            connection_target=portal_target if i == span - 1 else None))
    return Navmesh(cell_id, polys)


def bridge_bundle(cell_id: str = VILLAGE) -> CellBundle:
    """The village as a raised walkway, with load-bearing inferred for real."""
    from lobster.build.navmesh_inference import infer_load_bearing
    structure = deck_structure()
    navmesh = bridge_navmesh(cell_id)
    table = infer_load_bearing(navmesh, structure)
    return CellBundle(
        cell_id=cell_id,
        terrain=flat_terrain(cell_id),          # ground far below the deck
        navmesh=navmesh,
        structures=(structure,),
        load_bearing={structure.structure_id: table},
        provenance={"records": [cell_id, structure.structure_id]})


def deck_chunk_under(poly_index: int, structure: StructureVoxelData) -> int:
    """The micro-chunk holding the deck directly beneath a walkway polygon."""
    voxels_per_chunk_m = structure.chunk_size * 0.25       # 2 m
    cz = int((poly_index * BRIDGE_POLY_M + BRIDGE_POLY_M * 0.5) / voxels_per_chunk_m)
    cy = int((BRIDGE_DECK_Y - 0.125) / voxels_per_chunk_m)
    return structure.chunk_index(0, cy, cz)


def bridge_workspace() -> BundleWorkspace:
    ws = BundleWorkspace()
    ws.write(bridge_bundle())
    ws.write(plain_bundle(FIELD))
    ws.write(plain_bundle(KEEP))
    return ws


def bottleneck_navmesh(cell_id: str = VILLAGE, *, span: int = 8,
                       width: float = 1.2, length: float = 2.0) -> Navmesh:
    """A corridor too narrow for two bodies abreast.

    1.2 m against a 0.4 m agent radius: two agents side by side would need
    1.6 m of clear width, so the only way through is one at a time. This is the
    geometry the leader-leash test needs - a formation whose offsets simply
    cannot be honoured.
    """
    polys = []
    for i in range(span):
        z0, z1 = i * length, (i + 1) * length
        polys.append(NavPoly(
            poly_id=i,
            points=((0.0, z0), (width, z0), (width, z1), (0.0, z1)),
            y=0.0,
            neighbours=tuple(n for n in (i - 1, i + 1) if 0 <= n < span)))
    return Navmesh(cell_id, polys)


# ---------------------------------------------------------------------------
# Build-step fixtures
# ---------------------------------------------------------------------------

def write_vox_file(path: str, size: Tuple[int, int, int],
                   voxels: Sequence[Tuple[int, int, int, int]]) -> str:
    """Write a minimal MagicaVoxel file. Coordinates are Z-up, as .vox is.

    Small enough to be worth having rather than checking a binary blob into the
    repository, and it means the reader is tested against a file it did not
    write itself in the same breath.
    """
    import struct

    def chunk(cid: bytes, content: bytes, children: bytes = b"") -> bytes:
        return (cid + struct.pack("<ii", len(content), len(children))
                + content + children)

    header = struct.pack("<iii", *size)
    xyzi = struct.pack("<i", len(voxels)) + b"".join(bytes(v) for v in voxels)
    palette = b"".join(bytes([i, i, i, 255]) for i in range(256))
    body = (chunk(b"SIZE", header) + chunk(b"XYZI", xyzi)
            + chunk(b"RGBA", palette))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"VOX " + struct.pack("<i", 150) + chunk(b"MAIN", b"", body))
    return path


def slab_vox(path: str, side: int = 24, height: int = 2,
             material: int = 3) -> str:
    """A flat slab of terrain, `side` x `side` columns, `height` voxels deep."""
    return write_vox_file(
        path, (side, side, height),
        [(x, y, z, material) for x in range(side) for y in range(side)
         for z in range(height)])


def cube_vox(path: str, side: int = 8, material: int = 7) -> str:
    """A solid cube - the simplest possible structure."""
    return write_vox_file(
        path, (side, side, side),
        [(x, y, z, material) for x in range(side) for y in range(side)
         for z in range(side)])


class BuildWorkspace:
    """A temp world: packages, `.vox` art, a manifest, and an output directory."""

    def __init__(self) -> None:
        self.path = tempfile.mkdtemp(prefix="lobster-world-")
        self.art = os.path.join(self.path, "art")
        self.out = os.path.join(self.path, "cells")
        os.makedirs(self.art, exist_ok=True)

    def write_package(self, package_id: str,
                      ops: Sequence[Dict[str, Any]]) -> str:
        import json
        path = os.path.join(self.path, package_id + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(package_dict(package_id, ops), f)
        return path

    def write_manifest(self, cells: Sequence[Dict[str, Any]],
                       packages: Sequence[str], **extra: Any) -> str:
        import json
        raw = {"format": "lobster-manifest", "version": 1,
               "packages": [os.path.join(PACKAGE_DIR, "lobster_geometry.json"),
                            os.path.join(PACKAGE_DIR, "lobster_limb_state.json")]
                           + list(packages),
               "vox_dir": "art", "cells": list(cells)}
        raw.update(extra)
        path = os.path.join(self.path, "world.manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        return path

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "BuildWorkspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def demo_world_ops(*, one_way: bool = False, no_spawn: bool = False,
                   bad_zone_shape: bool = False, no_exterior_grid: bool = False,
                   structure_location: str = "cell-a") -> List[Dict[str, Any]]:
    """A two-cell world the build step can bake, with switchable defects."""
    # when cell-b has no default spawn, the door into it drops its spawn point
    # too - otherwise the connection still makes cell-b enterable and there is
    # nothing for the lint to find.
    a_connections = [dict({"target_location_id": "cell-b", "label": "east"},
                          **({} if no_spawn
                             else {"spawn_transform": spawn(2, 2, 2)}))]
    b_connections = ([] if one_way
                     else [{"target_location_id": "cell-a", "label": "west",
                            "spawn_transform": spawn(20, 2, 20)}])
    # Exterior cells declare where they sit on the grid (DECISIONS.md D21);
    # `no_exterior_grid` omits it so the lint can be tested firing.
    grid_a = {} if no_exterior_grid else {"exterior_grid": [0, 0]}
    grid_b = {} if no_exterior_grid else {"exterior_grid": [0, 1]}
    ops = [
        C("cell-a", "Location", display_name="A", tags=["exterior"],
          default_spawn_transform=spawn(4, 2, 4), connections=a_connections,
          **grid_a),
        C("cell-b", "Location", display_name="B", tags=["exterior"],
          **({} if no_spawn else {"default_spawn_transform": spawn(4, 2, 4)}),
          connections=b_connections, **grid_b),
        C("gatehouse", "StructureState", display_name="Gatehouse",
          location_id=structure_location, destroyed_chunks=[]),
    ]
    if bad_zone_shape:
        ops.append(C("zone-bad", "Zone", display_name="Bad",
                     location_refs=["cell-a"],
                     shape={"kind": "torus", "center": [0, 0, 0]}))
    return ops


def demo_manifest_cells(**extra_a: Any) -> List[Dict[str, Any]]:
    cell_a = {"location_id": "cell-a", "terrain_vox": "slab.vox",
              "terrain_side": 24,
              "structures": [{"structure_id": "gatehouse", "vox": "cube.vox",
                              "origin": {"position": [6, 2, 6],
                                         "rotation": [0, 0, 0, 1]}}]}
    cell_a.update(extra_a)
    return [cell_a,
            {"location_id": "cell-b", "terrain_vox": "slab.vox",
             "terrain_side": 24}]


# ---------------------------------------------------------------------------
# Two disjoint exterior clusters - Shrimp finding #6 (DECISIONS.md D32)
# ---------------------------------------------------------------------------
#
# A 4-connected ring is 5 cells, so two rings that share nothing sum to 10
# against a ceiling of 9. The standard world cannot show this: it has two
# exterior cells and they are neighbours.

HOME_CELL = "cell-home-1-1"
FAR_CELL = "cell-far-1-1"


def disjoint_cluster_ops(clusters=(("home", 0, 0), ("far", 10, 10)), side=3):
    """`side` x `side` patches of 4-connected exterior cells, far apart."""
    ops, ids = [], []
    for tag, ox, oz in clusters:
        for gx in range(side):
            for gz in range(side):
                cid = "cell-%s-%d-%d" % (tag, gx, gz)
                ids.append(cid)
                links = []
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, nz = gx + dx, gz + dz
                    if 0 <= nx < side and 0 <= nz < side:
                        links.append({
                            "target_location_id": "cell-%s-%d-%d" % (tag, nx, nz),
                            "label": "path",
                            "spawn_transform": spawn(64, 0, 64)})
                ops.append(C(cid, "Location", display_name=cid,
                             tags=["exterior"],
                             exterior_grid=[ox + gx, oz + gz],
                             connections=links,
                             default_spawn_transform=spawn(64, 0, 64)))
    return ops, ids


def disjoint_world():
    """A session and a workspace holding two clusters that share no ring."""
    ops, ids = disjoint_cluster_ops()
    session = build_session()
    for op in ops:
        session.engine.write(op)
    ws = BundleWorkspace()
    for cid in ids:
        ws.write(plain_bundle(cid))
    return session, ws, ids
