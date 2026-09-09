"""Lobster's hit-test tiers, and their exact relationship to Octopus's
simulation tiers (Scope 7, open question 16.1 - resolved, see DECISIONS.md D0).

Scope 7 made this a blocking check:

> **Tier names, deliberately Lobster's own - not assumed to match Octopus's
> simulation tiers.** [...] **This still needs a direct check against
> `nested-object-engine-sds.md` before any code lands** - if Octopus's real
> simulation tiers share a name with the tier below, adopt that name for the
> shared concept.

The check was done. Octopus declares ACTIVE / NEARBY / DORMANT
(`lce/npc.py`, SDS 9.2). So:

* `ACTIVE` and `DORMANT` are the *same concept* in both systems, and Lobster
  adopts Octopus's spelling. The assertions at the bottom of this module make a
  future rename in Octopus a loud import-time failure here, not a silent drift.
* `PROJECTILE` is Lobster's own, and answers a question Octopus never asks:
  "is this entity a geometry candidate for a hit-test", not "how much is this
  entity being simulated". Octopus's middle tier `NEARBY` means "adjacent per
  the connections graph"; a fireball can cross cells that are not adjacent at
  all. Asserting `PROJECTILE` is none of Octopus's tier names keeps the two
  vocabularies from converging by accident later.

The load-bearing consequence, from the same section:

> **PROJECTILE is a hit-test-candidacy tier, not a simulation-promotion tier**

`octopus_tier_for` is where that is enforced: only Lobster's ACTIVE maps onto
Octopus's ACTIVE. PROJECTILE and DORMANT map to None, which Octopus documents
(DESIGN_NOTES D24) as the *safe* value - attack/flee/hide activities are
skipped for it. Being shot does not wake anybody up.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .octopus_path import ensure_lce_importable

ensure_lce_importable()

from lce.npc import TIER_ACTIVE as _OCTOPUS_ACTIVE  # noqa: E402
from lce.npc import TIER_DORMANT as _OCTOPUS_DORMANT  # noqa: E402
from lce.npc import TIER_NEARBY as _OCTOPUS_NEARBY  # noqa: E402

#: Current cell + immediate melee range. Full skeleton, 6-region hit test.
#: Same concept as Octopus's actively-simulated tier - confirmed, not assumed.
ACTIVE = "ACTIVE"

#: Within range of a projectile *currently being resolved*, in whatever cell -
#: regardless of connection-graph adjacency. Lobster-only.
#:
#: Scoped by the attack in flight, not by distance to the player: a fireball two
#: connections away makes distant NPCs candidates for that one test, and they
#: are not "in PROJECTILE mode" before it is cast or after it lands. This is a
#: tier, not a creature type - see TIER_KIND and DECISIONS.md D18.
PROJECTILE = "PROJECTILE"

#: Everything else resident. No hitbox at all; effects reach these entities
#: through the occupancy + Event path in Scope 6.5.
DORMANT = "DORMANT"

TIERS: Tuple[str, ...] = (ACTIVE, PROJECTILE, DORMANT)

#: Hitbox fidelity per tier. PROJECTILE departs from the Scope 7 table's
#: "single whole-body capsule" by design - the capsule decides whether it hit,
#: and a per-bone pass then decides where (DECISIONS.md D16).
FIDELITY = {
    ACTIVE: "full_skeleton",
    PROJECTILE: "whole_body_capsule_then_regions",
    DORMANT: "none",
}

#: What a tier IS, for anyone reading the surface rather than the prose.
#: Shrimp's first scope draft read PROJECTILE as a lightweight creature class;
#: it is nothing of the kind (DECISIONS.md D18).
TIER_KIND = ("per-entity, per-cell-residency runtime state set by the game - "
             "not an entity class, archetype or LOD level. It answers 'how "
             "precisely can this entity be hit right now'.")

#: The tier for an entity that is present but is not a hit-test candidate.
#: Needs no rig and costs nothing; the right default for a mob nobody is
#: fighting. Effects still reach it through Scope 6.5's occupancy + Event path.
CHEAPEST_TIER = DORMANT


class TierError(Exception):
    """A tier value that is not one of Lobster's three."""


def check(tier: str) -> str:
    if tier not in TIERS:
        raise TierError(
            "{0!r} is not a Lobster hit-test tier; the set is {1}".format(
                tier, list(TIERS)))
    return tier


def octopus_tier_for(tier: str) -> Optional[str]:
    """The tier value to hand Octopus for an entity at this Lobster tier.

    ACTIVE  -> "ACTIVE"  (the shared concept)
    PROJECTILE -> None   (candidate for a hit test, NOT promoted)
    DORMANT -> None      (Octopus's own dormant state, unchanged)

    None is Octopus's documented safe value: `resolve_npc_state` treats an
    unspecified tier as not-ACTIVE and skips ACTIVE-only activities.
    """
    check(tier)
    return _OCTOPUS_ACTIVE if tier == ACTIVE else None


def has_hitbox(tier: str) -> bool:
    return check(tier) != DORMANT


# ---------------------------------------------------------------------------
# The 16.1 check, mechanised. These run at import.
# ---------------------------------------------------------------------------

assert ACTIVE == _OCTOPUS_ACTIVE, (
    "Octopus renamed its ACTIVE tier to {0!r}. Scope 7 says to adopt Octopus's "
    "name for the shared concept - update lobster.tiers.ACTIVE and every "
    "consumer, deliberately.".format(_OCTOPUS_ACTIVE))

assert DORMANT == _OCTOPUS_DORMANT, (
    "Octopus renamed its DORMANT tier to {0!r}; same rule as ACTIVE.".format(
        _OCTOPUS_DORMANT))

assert PROJECTILE not in (_OCTOPUS_ACTIVE, _OCTOPUS_NEARBY, _OCTOPUS_DORMANT), (
    "Lobster's middle tier has collided with an Octopus simulation tier "
    "({0!r}). That collision is the migration debt Scope v0.3 renamed it to "
    "avoid - pick a name Octopus does not use.".format(PROJECTILE))
