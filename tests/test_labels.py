"""World-space labels (Scope §8 step 4, DECISIONS.md D37).

Most of this file is about what labels **refuse** to do. The geometry is four
numbers and a boolean; the discipline is that Lobster stops there and hands the
screen position to whoever owns 2D chrome.
"""

from __future__ import annotations

import unittest

from lobster.camera import Camera
from lobster.cell import CellManager
from lobster.geometry import Transform
from lobster.labels import (ENTITY_LABEL_LIFT_M, LabelAnchor, is_occluded,
                            label_anchors, locate)
from lobster.octopus_bridge import OctopusBridge
from lobster.selection import ENTITY, ITEM, STRUCTURE, Selector
from lobster.skeleton import Skeleton, humanoid_region_set
from lobster.tiers import ACTIVE, PROJECTILE
from tests.fixtures import (FIELD, GATEHOUSE, VILLAGE, build_session,
                            standard_workspace)


class LabelFixture(unittest.TestCase):

    def setUp(self):
        self.session = build_session()
        self.bridge = OctopusBridge(self.session)
        self.ws = standard_workspace()
        self.addCleanup(self.ws.close)
        self.manager = CellManager(self.ws.path, session=self.session)
        self.view = self.bridge.frame()
        self.manager.set_player_cell(self.view, VILLAGE)
        self.village = self.manager.resident[VILLAGE]

    def stand(self, entity_id, local, cell=None, tier=ACTIVE):
        (cell or self.village).place(
            entity_id, local, tier,
            skeleton=Skeleton(entity_id, humanoid_region_set(),
                              root=Transform(position=local)))

    def drop(self, item_id, cell_id, at):
        self.session.engine.write({"op": "CREATE", "record": {
            "id": item_id, "type": "Item", "display_name": item_id}})
        self.manager.place_item(self.view, item_id, cell_id,
                                Transform(position=at))
        self.view = self.bridge.frame()

    def selector(self):
        return Selector.from_manager(self.manager, self.view)

    def camera(self, eye=(6.0, 1.6, 0.0), at=(6.0, 1.0, 20.0)):
        return Camera.looking_at(eye, at)

    def anchors(self, targets, **kwargs):
        kwargs.setdefault("view", self.view)
        return label_anchors(self.camera(), self.selector(), targets, **kwargs)


class TestAnchoringEachKind(LabelFixture):

    def test_an_entity_anchors_above_its_rig(self):
        self.stand("ada", (6.0, 0.0, 6.0))
        anchor = self.anchors(["ada"])[0]
        self.assertEqual((anchor.kind, anchor.cell_id), (ENTITY, VILLAGE))

        capsule = self.village.skeletons["ada"].whole_body_capsule()
        top = max(capsule.a[1], capsule.b[1])
        self.assertAlmostEqual(anchor.world_point[1],
                               top + capsule.radius + ENTITY_LABEL_LIFT_M,
                               places=5)

    def test_the_lift_is_derived_from_the_rig_not_from_a_humanoid(self):
        """A label over a spider belongs over the spider - the same reasoning
        that made the projectile gate rig-derived (D19)."""
        self.stand("ada", (6.0, 0.0, 6.0))
        tall = Skeleton("giant", humanoid_region_set(),
                        root=Transform(position=(9.0, 0.0, 6.0)))
        tall.set_root(Transform(position=(9.0, 3.0, 6.0)))
        self.village.place("giant", (9.0, 3.0, 6.0), ACTIVE, skeleton=tall)
        self.view = self.bridge.frame()

        found = {a.target_id: a for a in self.anchors(["ada", "giant"])}
        self.assertGreater(found["giant"].world_point[1],
                           found["ada"].world_point[1] + 2.0)

    def test_a_structure_anchors_above_its_top(self):
        anchor = self.anchors([GATEHOUSE])[0]
        self.assertEqual(anchor.kind, STRUCTURE)
        box = self.village.structure(GATEHOUSE).voxel_data.aabb_world()
        self.assertGreater(anchor.world_point[1], box.maximum[1],
                           "anchoring inside the structure would put the label "
                           "behind its own wall on every approach")

    def test_an_item_anchors_just_above_where_it_lies(self):
        self.drop("item-sword", VILLAGE, (6.0, 0.0, 8.0))
        anchor = self.anchors(["item-sword"])[0]
        self.assertEqual(anchor.kind, ITEM)
        self.assertGreater(anchor.world_point[1], 0.0)
        self.assertLess(anchor.world_point[1], 1.0)

    def test_an_item_needs_a_view_like_everywhere_else(self):
        self.drop("item-sword", VILLAGE, (6.0, 0.0, 8.0))
        self.assertEqual(self.anchors(["item-sword"], view=None), [])


class TestProjection(LabelFixture):

    def test_screen_point_is_pixels_from_the_top_left(self):
        self.stand("ada", (6.0, 0.0, 6.0))
        anchor = self.anchors(["ada"])[0]
        x, y = anchor.screen_point
        self.assertTrue(0.0 <= x <= 1280.0, x)
        self.assertTrue(0.0 <= y <= 720.0, y)
        self.assertTrue(anchor.on_screen)

    def test_behind_the_camera_is_not_projectable_and_says_so(self):
        """`None` means "at or behind the near plane", which is a different
        answer from "off screen" - and the distance is still real."""
        self.stand("ada", (6.0, 0.0, 6.0))
        behind = label_anchors(self.camera(eye=(6.0, 1.6, 20.0),
                                           at=(6.0, 1.6, 40.0)),
                               self.selector(), ["ada"], view=self.view)
        self.assertIsNone(behind[0].screen_point)
        self.assertFalse(behind[0].on_screen)
        self.assertGreater(behind[0].distance, 0.0)

    def test_off_screen_still_projects(self):
        """An edge-of-screen marker is a real use, so a projectable point
        outside the viewport comes back with its coordinates rather than None."""
        self.stand("ada", (60.0, 0.0, 6.0))
        anchor = self.anchors(["ada"])[0]
        self.assertIsNotNone(anchor.screen_point)
        self.assertGreater(anchor.screen_point[0], 1280.0)

    def test_the_viewport_size_is_the_callers(self):
        self.stand("ada", (6.0, 0.0, 6.0))
        wide = self.anchors(["ada"], width=1920, height=1080)[0]
        small = self.anchors(["ada"], width=640, height=360)[0]
        self.assertAlmostEqual(wide.screen_point[0], small.screen_point[0] * 3.0,
                               places=3)


class TestOcclusion(LabelFixture):
    """Shares `Selector`, so labels and picking can never disagree."""

    def test_a_wall_hides_the_label_behind_it(self):
        self.stand("ada", (2.0, 0.0, 20.0))
        camera = self.camera(eye=(2.0, 1.6, 0.0), at=(2.0, 1.6, 20.0))
        anchors = label_anchors(camera, self.selector(), ["ada"],
                                view=self.view)
        self.assertTrue(anchors[0].occluded,
                        "the gatehouse stands between the camera and ada")

    def test_clear_line_of_sight_is_not_occluded(self):
        self.stand("ada", (6.0, 0.0, 6.0))
        self.assertFalse(self.anchors(["ada"])[0].occluded)

    def test_a_label_is_never_hidden_by_its_own_subject(self):
        """The anchor floats just above the thing it labels, so a ray reaching
        it can clip the very shoulder it hangs over. A label hidden by its own
        subject would be a defect dressed as a feature."""
        self.stand("ada", (6.0, 0.0, 6.0))
        selector = self.selector()
        anchor = self.anchors(["ada"])[0]
        self.assertFalse(is_occluded(self.camera(), selector,
                                     anchor.world_point, "ada",
                                     view=self.view))

    def test_labels_and_picking_agree_about_what_is_in_front(self):
        """The property that made sharing `Selector` worth it."""
        self.stand("ada", (2.0, 0.0, 20.0))
        camera = self.camera(eye=(2.0, 1.6, 0.0), at=(2.0, 1.6, 20.0))
        selector = self.selector()
        picked = selector.pick(camera.position, (0.0, 0.0, 1.0), 40.0,
                               view=self.view)
        anchor = label_anchors(camera, selector, ["ada"], view=self.view)[0]
        self.assertNotEqual(picked.target_id, "ada")
        self.assertTrue(anchor.occluded,
                        "the crosshair says a wall is in the way; the label "
                        "must not claim otherwise")

    def test_occlusion_can_be_skipped(self):
        """It is the whole cost of the call, and an edge marker does not care
        what is in front of the thing it points at."""
        self.stand("ada", (2.0, 0.0, 20.0))
        camera = self.camera(eye=(2.0, 1.6, 0.0), at=(2.0, 1.6, 20.0))
        anchors = label_anchors(camera, self.selector(), ["ada"],
                                view=self.view, test_occlusion=False)
        self.assertFalse(anchors[0].occluded)


class TestWhatLabelsRefuseToDecide(LabelFixture):
    """L4/L8, structurally - the same line selection and items hold."""

    def test_there_is_no_text_anywhere(self):
        for banned in ("text", "display_name", "name", "caption", "tooltip",
                       "localised", "font"):
            self.assertFalse(hasattr(LabelAnchor, banned), banned)

    def test_there_is_no_should_draw_and_no_priority(self):
        for banned in ("visible", "should_draw", "priority", "importance",
                       "alpha", "fade", "declutter", "layer"):
            self.assertFalse(hasattr(LabelAnchor, banned), banned)

    def test_the_module_never_reads_a_display_name(self):
        """§5 invariant 3: nothing in `lobster/` names a creature or an item."""
        import inspect
        from lobster import labels
        source = inspect.getsource(labels)
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith(("#", ">", "*")))
        self.assertNotIn('get("display_name")', code)
        self.assertNotIn("display_name", code.split('"""')[-1])

    def test_overlapping_labels_are_not_resolved_here(self):
        """Two anchors on the same pixel come back as two anchors. Which one
        survives is layout, and layout is 2D chrome."""
        self.stand("ada", (6.0, 0.0, 6.0))
        self.stand("bren", (6.0, 0.0, 6.2))
        found = self.anchors(["ada", "bren"])
        self.assertEqual(len(found), 2)
        self.assertLess(
            abs(found[0].screen_point[0] - found[1].screen_point[0]), 40.0,
            "the fixture must actually overlap or this proves nothing")

    def test_nearest_first_is_the_only_ordering(self):
        self.stand("far", (6.0, 0.0, 30.0))
        self.stand("near", (6.0, 0.0, 5.0))
        found = self.anchors(["far", "near"])
        self.assertEqual([a.target_id for a in found], ["near", "far"])
        self.assertEqual([a.distance for a in found],
                         sorted(a.distance for a in found))


class TestLocatingTargets(LabelFixture):

    def test_a_target_that_is_nowhere_is_omitted_not_raised(self):
        """An NPC walking out of the resident set is ordinary. Raising would
        make a caller handle an exception on a normal frame."""
        self.stand("ada", (6.0, 0.0, 6.0))
        found = self.anchors(["ada", "ghost-that-left"])
        self.assertEqual([a.target_id for a in found], ["ada"])
        self.assertIsNone(locate(self.selector(), "ghost-that-left",
                                 view=self.view))

    def test_a_target_in_another_cell_is_found_and_placed(self):
        self.stand("bandit", (2.0, 0.0, 4.0), cell=self.manager.resident[FIELD],
                   tier=PROJECTILE)
        self.view = self.bridge.frame()
        anchor = self.anchors(["bandit"])[0]
        self.assertEqual(anchor.cell_id, FIELD)
        self.assertGreater(anchor.world_point[2], 128.0,
                           "the anchor must come back in world space")

    def test_locate_reports_the_kind_it_found(self):
        self.stand("ada", (6.0, 0.0, 6.0))
        self.assertEqual(locate(self.selector(), "ada", view=self.view)[0],
                         ENTITY)
        self.assertEqual(locate(self.selector(), GATEHOUSE, view=self.view)[0],
                         STRUCTURE)


if __name__ == "__main__":
    unittest.main()
