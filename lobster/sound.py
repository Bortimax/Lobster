"""Ambient sound sources (Scope 3, 4.5).

> Where do ambient sound sources live -> **Cell metadata**,
> `{position, sound_id, radius}`. A mod-supplied list is ordinary layered
> content - same merge/quarantine rules as any other record, never a second save
> channel (4.5).

"Cell metadata" is satisfied by the fact that a cell *is* a Location: the list
lives on `Location.sound_sources`, declared `UNION_TOMBSTONED` by Lobster's
schema-extension package, so a mod adding a source is a `MERGE`, removing one is
a `DELETE_ENTRY`, and both quarantine and reconcile like anything else. Nothing
is baked into the bundle - DECISIONS.md D7 explains why a read-only baked copy
would still be the bypass channel 4.5 forbids.

What this module does is the geometry: parse the entries, and answer which
sources reach a given point. What it deliberately does not do is mix,
prioritise, duck, fade, choose a channel or decide what a source *means* - all
of which is policy, and L7 says the shell has none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .geometry import Vec3, distance, vec3


class SoundSourceError(Exception):
    """A malformed sound source. Names the cell and the entry."""


@dataclass(frozen=True)
class SoundSource:
    """One ambient emitter, exactly the shape Scope 3 declares."""

    sound_id: str
    position: Vec3
    radius: float
    cell_id: Optional[str] = None

    def reaches(self, point: Vec3) -> bool:
        return distance(self.position, point) <= self.radius

    def falloff_at(self, point: Vec3) -> float:
        """Linear 1..0 over the radius. Distance, not loudness.

        A renderer or an audio mixer decides what to do with a number like this
        - a curve, a rolloff model, a priority. Lobster reports how far away the
        listener is, in the units the content declared.
        """
        if self.radius <= 0.0:
            return 0.0
        d = distance(self.position, point)
        if d >= self.radius:
            return 0.0
        return 1.0 - (d / self.radius)

    def to_dict(self) -> Dict[str, Any]:
        return {"sound_id": self.sound_id, "position": list(self.position),
                "radius": self.radius, "cell_id": self.cell_id}


def parse_sources(entries: Iterable[Mapping[str, Any]], *,
                  cell_id: Optional[str] = None,
                  strict: bool = False) -> Tuple[List[SoundSource],
                                                 List[Dict[str, Any]]]:
    """Parse a `Location.sound_sources` list. Returns (sources, findings).

    A malformed entry is reported and skipped rather than raising, because the
    list is layered content: a mod can put anything in it, and one bad entry
    should cost that entry, not the cell. `strict=True` raises instead, which is
    what the build step wants.
    """
    sources: List[SoundSource] = []
    findings: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries or ()):
        try:
            if not isinstance(entry, Mapping):
                raise ValueError("entry must be an object")
            sound_id = entry.get("sound_id")
            if not isinstance(sound_id, str) or not sound_id:
                raise ValueError("sound_id must be a non-empty string")
            radius = float(entry.get("radius", 0.0))
            if radius <= 0.0:
                raise ValueError("radius must be positive")
            sources.append(SoundSource(
                sound_id=sound_id,
                position=vec3(entry.get("position"), what="position"),
                radius=radius, cell_id=cell_id))
        except (TypeError, ValueError) as e:
            finding = {"code": "malformed_sound_source", "cell_id": cell_id,
                       "record_id": cell_id, "index": index,
                       "detail": "sound_sources[{0}]: {1}".format(index, e)}
            if strict:
                raise SoundSourceError(finding["detail"]) from e
            findings.append(finding)
    return sources, findings


def sources_for_cell(cell: Any, *,
                     strict: bool = False) -> Tuple[List[SoundSource],
                                                    List[Dict[str, Any]]]:
    """A resident cell's ambient sources, read live off its Location record."""
    return parse_sources(cell.sound_sources(), cell_id=cell.cell_id,
                         strict=strict)


def audible(sources: Sequence[SoundSource], listener: Vec3) -> List[SoundSource]:
    """Which sources reach the listener, nearest first.

    Sorted by distance then id, so two runs of the same frame produce the same
    list - the same determinism rule Octopus holds itself to.
    """
    reaching = [s for s in sources if s.reaches(listener)]
    reaching.sort(key=lambda s: (distance(s.position, listener), s.sound_id))
    return reaching


def report(sources: Sequence[SoundSource]) -> Dict[str, Any]:
    return {"count": len(sources),
            "sources": [s.to_dict() for s in sources]}
