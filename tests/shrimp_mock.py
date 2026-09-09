"""A mock of Shrimp's animation controller.

Scope 5:

> Shrimp's animation controller reads the identical field, via the identical
> query, and decides what to do with it. Both systems reading the same source of
> truth independently is the correct boundary - not Lobster reading it once and
> pushing a decision downstream.

So this mock deliberately does **not** import anything from
`lobster.octopus_bridge`, does not hold a `FrameView`, and never touches the
`HIT_TEST_ONLY` sentinel. It reads `Character.limb_state` off the resolution
itself, the way a separate repository would, and makes its own decision about
what to pose. If a future refactor makes Lobster hand the animation controller a
limb decision, this mock will keep passing while the real boundary rots - so it
also asserts, in the test, that Lobster did not call it.

It is a *mock*, not a specification of Shrimp. All it does is decide which bones
it would pose, which is enough to prove the two systems reached the same
conclusion separately.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

SEVERED = "severed"


class MockAnimationController:
    """Poses every bone whose limb is not severed. Its own reader, its own rule."""

    def __init__(self, session: Any, skeleton: Any) -> None:
        self.session = session
        self.skeleton = skeleton
        self.posed_bones: List[str] = []
        self.reads = 0

    def _limb_state(self, entity_id: str) -> Dict[str, str]:
        """The identical field, via the identical resolved-record read."""
        self.reads += 1
        record = self.session.resolution().get(entity_id)
        if record is None:
            return {}
        raw = record.get("limb_state")
        return dict(raw) if isinstance(raw, dict) else {}

    def bones_to_pose(self, entity_id: str) -> List[str]:
        """Shrimp's decision, made by Shrimp.

        Lobster is not consulted, does not know this happened, and could not
        influence it if it wanted to.
        """
        states = self._limb_state(entity_id)
        out: List[str] = []
        for bone in self.skeleton.region_set.bones:
            if bone.limb_id and states.get(bone.limb_id) == SEVERED:
                continue
            out.append(bone.bone_id)
        return out

    def tick(self, entity_id: str) -> List[str]:
        self.posed_bones = self.bones_to_pose(entity_id)
        return self.posed_bones
