"""Characters drawn as real geometry: a model per bone, rigid, no skinning.

Every other thing in the world draws as a mesh - terrain, structures, props,
items. Entities never have: the software path drew one flat capsule per bone
and the GPU path a single box, and nothing connected a `Model` to a bone. This
is that closed, and the shape of the answer is the project owner's:

> "we don't need anything to 'bend' ever."

Which removes the entire reason characters looked expensive. A bone wears a
model; the model sits at the bone's transform; the same composition that moves
the hitbox moves the model. No vertex weights, no bone matrices in a shader, no
second authoring format.

These are pixel tests for the reason ASSET_SCOPE §7 step 4 gives - the software
path is the only oracle for *wrong*, since the two backends are deliberately
not pixel-compared - plus the three ways a worn model can be put in the wrong
place: the bone's pose, the entity's facing, and the cell's placement.
"""

from __future__ import annotations

import math
import unittest

from lobster.budgets import Budget
from lobster.camera import Camera
from lobster.cell import ResidentCell
from lobster.geometry import Transform
from lobster.render import RenderSettings
from lobster.render.raster import (Framebuffer, _draw_entity,
                                   render_draw_list)
from lobster.skeleton import (Skeleton, SkeletonError, Wardrobe,
                              humanoid_region_set)
from lobster.tiers import ACTIVE
from lobster.visibility import ENTITY, build_draw_list
from tests.fixtures import plain_bundle, primitive_library, prop
from tests.test_model_drawing import Pixels

CELL = "cell-look"
HELM = "model-crate"
MAIL = "model-barrel"
BADGE = "model-sign"

#: a full suit, so a dressed entity has no bone left to impostor
FULL = {bone.bone_id: HELM for bone in humanoid_region_set().bones}


def turn(degrees):
    half = math.radians(degrees / 2.0)
    return (0.0, math.sin(half), 0.0, math.cos(half))


class CharacterFixture(unittest.TestCase):

    WIDTH, HEIGHT = 240, 135

    def settings(self):
        return RenderSettings(width=self.WIDTH, height=self.HEIGHT)

    def cell(self):
        return ResidentCell(plain_bundle(CELL), Budget.declared(CELL, {}))

    def camera(self, eye=(6.0, 1.6, 7.0), target=(6.0, 1.0, 10.0)):
        return Camera.looking_at(eye, target).with_aspect(
            self.settings().aspect())

    def stand(self, cell, entity_id="npc", position=(6.0, 0.0, 10.0),
              wardrobe=None, rotation=(0.0, 0.0, 0.0, 1.0)):
        skeleton = Skeleton(entity_id, humanoid_region_set(),
                            root=Transform(position=position,
                                           rotation=rotation),
                            wardrobe=wardrobe)
        cell.place(entity_id, position, ACTIVE, skeleton=skeleton)
        return skeleton

    def draw_list(self, cell, *, placements=None, library=...):
        library = primitive_library() if library is ... else library
        return build_draw_list(self.camera(), [cell], placements=placements,
                               library=library)

    def render(self, cell, *, placements=None, library=...):
        library = primitive_library() if library is ... else library
        settings = self.settings()
        frame = render_draw_list(
            self.draw_list(cell, placements=placements, library=library),
            {cell.cell_id: cell}, settings=settings, library=library)
        return Pixels(frame, settings)

    def empty(self):
        """The same cell with nobody in it."""
        return self.render(self.cell())


# ---------------------------------------------------------------------------
# Geometry, not capsules
# ---------------------------------------------------------------------------

class TestADressedEntityDrawsGeometry(CharacterFixture):

    def test_a_wardrobe_changes_what_reaches_the_screen(self):
        bare = self.cell()
        self.stand(bare)
        dressed = self.cell()
        self.stand(dressed, wardrobe=Wardrobe("guard", FULL))

        baseline = self.empty()
        one = self.render(bare).drawn(baseline)
        two = self.render(dressed).drawn(baseline)
        self.assertTrue(one, "the bare rig drew nothing, so there is nothing "
                             "to compare against")
        self.assertTrue(two, "the dressed rig drew nothing")
        self.assertNotEqual(sorted(one), sorted(two),
                            "dressing the entity changed no pixels")

    def test_a_dressed_bone_produces_a_model_draw_item(self):
        cell = self.cell()
        self.stand(cell, wardrobe=Wardrobe("guard", {"head": HELM}))
        worn = [i for i in self.draw_list(cell).items
                if i.kind == ENTITY and i.model_ref]
        self.assertEqual([i.model_ref for i in worn], [HELM])
        self.assertEqual(worn[0].item_id, "npc/head",
                         "a worn model has to say which bone it is")

    def test_a_fully_dressed_entity_emits_no_impostor(self):
        """Otherwise the capsules draw through the models."""
        cell = self.cell()
        self.stand(cell, wardrobe=Wardrobe("guard", FULL))
        bare = [i for i in self.draw_list(cell).items
                if i.kind == ENTITY and not i.model_ref]
        self.assertEqual(bare, [])

    def test_a_partly_dressed_entity_still_emits_one(self):
        cell = self.cell()
        self.stand(cell, wardrobe=Wardrobe("guard", {"head": HELM}))
        bare = [i for i in self.draw_list(cell).items
                if i.kind == ENTITY and not i.model_ref]
        self.assertEqual([i.item_id for i in bare], ["npc"],
                         "the undressed bones still need their impostor")

    def test_an_undressed_entity_is_unchanged(self):
        """The impostor path is not going away, and this is the proof that
        nothing about it moved."""
        cell = self.cell()
        self.stand(cell)
        kinds = [(i.kind, i.item_id, i.model_ref)
                 for i in self.draw_list(cell).items if i.kind == ENTITY]
        self.assertEqual(kinds, [(ENTITY, "npc", "")])


# ---------------------------------------------------------------------------
# Where the model goes
# ---------------------------------------------------------------------------

class TestTheModelGoesWhereTheBoneGoes(CharacterFixture):

    def test_what_you_see_is_what_you_hit(self):
        """The property the whole design rests on. A worn model is placed by
        `root.compose(local)` and so is the bone's hitbox, so the thing on
        screen and the thing a shot resolves against cannot drift apart.
        """
        skeleton = Skeleton("npc", humanoid_region_set(),
                            root=Transform(position=(3.0, 0.0, 4.0),
                                           rotation=turn(37.0)),
                            wardrobe=Wardrobe("guard", FULL))
        skeleton.set_pose({"head": Transform(position=(0.0, 0.15, 0.0),
                                             rotation=turn(20.0))})
        placed = {bone_id: t for bone_id, _ref, t in skeleton.worn_models()}
        for bone in skeleton.region_set.bones:
            with self.subTest(bone=bone.bone_id):
                capsule = skeleton.capsule_for(bone.bone_id)
                transform = placed[bone.bone_id]
                for got, want in zip(transform.apply(bone.a), capsule.a):
                    self.assertAlmostEqual(got, want, places=9)
                for got, want in zip(transform.apply(bone.b), capsule.b):
                    self.assertAlmostEqual(got, want, places=9)

    def test_posing_a_bone_moves_its_model(self):
        cell = self.cell()
        skeleton = self.stand(cell, wardrobe=Wardrobe("guard", {"head": HELM}))
        before = next(i for i in self.draw_list(cell).items if i.model_ref)

        skeleton.set_pose({"head": Transform(position=(0.0, 1.0, 0.0))})
        after = next(i for i in self.draw_list(cell).items if i.model_ref)

        self.assertAlmostEqual(after.transform.position[1]
                               - before.transform.position[1], 1.0, places=9)
        self.assertGreater(after.center[1], before.center[1],
                           "the model moved in the draw item but not in the "
                           "world centre the renderer culls against")

    def test_turning_the_entity_turns_its_models(self):
        """An offset bone, so the rotation is visible at all - a model on the
        axis the entity turns about lands in the same place either way."""
        arm = next(b for b in humanoid_region_set().bones
                   if abs(b.a[0]) > 1e-6 or abs(b.a[2]) > 1e-6)
        wardrobe = Wardrobe("guard", {arm.bone_id: HELM})

        facing = self.cell()
        self.stand(facing, wardrobe=wardrobe)
        turned = self.cell()
        self.stand(turned, wardrobe=wardrobe, rotation=turn(90.0))

        a = next(i for i in self.draw_list(facing).items if i.model_ref)
        b = next(i for i in self.draw_list(turned).items if i.model_ref)
        self.assertNotAlmostEqual(a.transform.rotation[1],
                                  b.transform.rotation[1], places=6)

        # Not the item's centre: every worn model shares the entity's origin
        # as its instance position, because the offset from origin to shoulder
        # lives in the *mesh* - which is what lets two guards standing in the
        # same pose share one instance. So what has to move is the geometry,
        # and the bone's rest endpoint is where that is measurable.
        here = a.transform.apply(arm.a)
        there = b.transform.apply(arm.a)
        moved = sum((here[i] - there[i]) ** 2 for i in range(3)) ** 0.5
        self.assertGreater(moved, 0.1,
                           "turning the entity did not move its arm")
        # Relative to the entity's own origin, which is what it turns about.
        root = (6.0, 0.0, 10.0)
        here_rel = tuple(here[i] - root[i] for i in range(3))
        there_rel = tuple(there[i] - root[i] for i in range(3))
        self.assertAlmostEqual(there_rel[0], here_rel[2], places=9)
        self.assertAlmostEqual(there_rel[2], -here_rel[0], places=9)


# ---------------------------------------------------------------------------
# The cell's placement
# ---------------------------------------------------------------------------

class TestTheCellsPlacementIsApplied(CharacterFixture):
    """A rig is posed in its *cell's* space, like a structure origin. The
    impostor path ignored that entirely - invisible in the origin cell, wrong
    in every other one, which is the mistake D21 and D23 found in the hit-test
    path and which survived here because no test drew an entity in a placed
    cell."""

    #: sideways, and small enough to keep the entity in shot. A 40 m shift
    #: pushed it off screen, and an assertion guarded by "if it is still
    #: visible" is an assertion that does not run.
    ASIDE = {CELL: Transform(position=(1.5, 0.0, 0.0))}
    FAR = {CELL: Transform(position=(0.0, 0.0, 40.0))}

    def test_a_worn_model_moves_with_its_cell(self):
        cell = self.cell()
        self.stand(cell, wardrobe=Wardrobe("guard", {"head": HELM}))
        here = next(i for i in self.draw_list(cell).items if i.model_ref)
        there = next(i for i in self.draw_list(cell, placements=self.FAR).items
                     if i.model_ref)
        self.assertAlmostEqual(there.center[2] - here.center[2], 40.0,
                               places=6)

    def where_is_the_entity(self, placements):
        """The entity's centroid, against the *same* cell without it.

        The baseline has to carry the same placement. Diffing a placed cell
        against an unplaced one makes the terrain the difference - it is a huge
        single colour and it moved too - and the centroid then shifts whether
        or not the entity did. `Pixels.drawn` carries a comment about this
        exact mistake costing a mutant; it costs this one as well.
        """
        peopled = self.cell()
        self.stand(peopled)
        empty = Pixels(*self._frame(self.cell(), placements))
        return Pixels(*self._frame(peopled, placements)).centroid(empty)

    def test_an_impostor_moves_with_its_cell_too(self):
        here = self.where_is_the_entity(None)
        there = self.where_is_the_entity(self.ASIDE)
        self.assertIsNotNone(here, "the entity drew nothing at the origin")
        self.assertIsNotNone(
            there, "the entity left the screen, so there is nothing to "
                   "compare against")
        self.assertGreater(
            abs(here[0] - there[0]), 2.0,
            "moving the cell sideways did not move the entity on screen")

    def _frame(self, cell, placements):
        library = primitive_library()
        settings = self.settings()
        frame = render_draw_list(
            build_draw_list(self.camera(), [cell], placements=placements,
                            library=library),
            {cell.cell_id: cell}, settings=settings, library=library)
        return frame, settings


# ---------------------------------------------------------------------------
# What it refuses, and what it falls back to
# ---------------------------------------------------------------------------

class TestWhatItRefusesAndFallsBackTo(CharacterFixture):

    def test_a_wardrobe_dressing_an_unknown_bone_is_refused(self):
        with self.assertRaises(SkeletonError) as ctx:
            Skeleton("npc", humanoid_region_set(),
                     wardrobe=Wardrobe("beast", {"tail": HELM}))
        self.assertIn("tail", str(ctx.exception))
        self.assertIn("humanoid", str(ctx.exception))

    def test_an_empty_ref_is_an_absence_not_a_fault(self):
        """The same rule a prop has: an empty `model_ref` is the impostor, and
        asking the library for "" would turn a documented absence into a
        reported fault."""
        wardrobe = Wardrobe("guard", {"head": HELM, "torso": ""})
        self.assertEqual(wardrobe.model_refs(), [HELM])
        self.assertIsNone(wardrobe.model_for("torso"))

    def test_a_worn_model_the_library_lacks_falls_back_to_that_bone(self):
        """Exactly that bone's capsule, and only it.

        Dressing one bone against a library that holds nothing must look
        *identical* to not dressing it - every bone ends up an impostor either
        way. A fallback that skipped the bone, or that redrew the other five,
        would both pass a test that only asked whether something drew.
        """
        bare = self.cell()
        self.stand(bare)
        dressed = self.cell()
        self.stand(dressed, wardrobe=Wardrobe("guard", {"head": HELM}))

        empty_library = primitive_library(MAIL)
        baseline = self.empty()
        one = self.render(bare, library=empty_library).drawn(baseline)
        two = self.render(dressed, library=empty_library).drawn(baseline)
        self.assertTrue(one, "the bare rig drew nothing")
        self.assertEqual(sorted(one), sorted(two),
                         "a bone whose model is missing did not fall back to "
                         "the capsule the undressed rig draws")

    def test_model_refs_are_what_the_entity_needs_resident(self):
        skeleton = Skeleton("npc", humanoid_region_set(),
                            wardrobe=Wardrobe("guard", {"head": HELM,
                                                        "torso": MAIL,
                                                        "arm_l": HELM}))
        self.assertEqual(skeleton.model_refs(), sorted({HELM, MAIL}))

    def test_an_undressed_entity_needs_nothing(self):
        self.assertEqual(Skeleton("npc").model_refs(), [])


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

class TestTheDrawListAccounting(CharacterFixture):

    def test_every_worn_model_is_charged_as_a_placement(self):
        """A character's arm costs what a barrel costs. If worn models were
        free the per-cell ceiling would stop meaning anything the moment a
        crowd turned up."""
        bare = self.cell()
        self.stand(bare)
        dressed = self.cell()
        self.stand(dressed, wardrobe=Wardrobe("guard", FULL))

        before = self.draw_list(bare).stats.placements_drawn
        after = self.draw_list(dressed).stats.placements_drawn
        self.assertEqual(after - before, len(humanoid_region_set().bones))

    def test_two_identical_guards_share_one_instance_buffer(self):
        """The reason this goes through the same path props do."""
        cell = self.cell()
        self.stand(cell, "npc-a", (5.0, 0.0, 10.0),
                   wardrobe=Wardrobe("guard", {"head": HELM}))
        self.stand(cell, "npc-b", (7.0, 0.0, 10.0),
                   wardrobe=Wardrobe("guard", {"head": HELM}))
        instances = self.draw_list(cell).instances
        keys = [k for k in instances if k[1] == HELM]
        self.assertEqual(len(keys), 1,
                         "two guards in one cell made two buffers")


class TestADressedBoneIsNotAlsoACapsule(CharacterFixture):
    """A capsule drawn through a model is a grey slab inside the mesh.

    Pixels cannot see this: the model is drawn at the bone, so it *covers* the
    capsule it would be hiding, and the two renders can come out identical
    while the renderer does twice the work and puts geometry inside geometry
    wherever the model happens to be thinner. So this counts draws instead -
    the one question is whether the impostor pass skips a dressed bone.
    """

    def capsules_for(self, wardrobe):
        """How many triangles the impostor pass emits for this wardrobe."""
        settings = self.settings()
        frame = Framebuffer(settings.width, settings.height,
                            background=settings.background)
        skeleton = Skeleton("npc", humanoid_region_set(),
                            root=Transform(position=(6.0, 0.0, 10.0)),
                            wardrobe=wardrobe)
        return _draw_entity(frame, self.camera(), skeleton, Transform(),
                            settings)

    def test_an_undressed_rig_draws_every_bone(self):
        bones = len(humanoid_region_set().bones)
        self.assertEqual(self.capsules_for(None), bones * 2,
                         "two triangles per bone is what the impostor is")

    def test_a_full_suit_draws_no_capsules_at_all(self):
        self.assertEqual(self.capsules_for(Wardrobe("guard", FULL)), 0)

    def test_only_the_undressed_bones_are_drawn(self):
        bones = len(humanoid_region_set().bones)
        one = Wardrobe("guard", {"head": HELM, "torso": MAIL})
        self.assertEqual(self.capsules_for(one), (bones - 2) * 2)


class TestWornModelsShareTheCellWithProps(CharacterFixture):
    """Instances are keyed by (cell, model), and a prop and a guard can name
    the same model. Whoever writes second must not erase the first."""

    CRATE_AT = (5.0, 0.0, 9.0)

    def with_prop(self, dressed):
        cell = ResidentCell(
            plain_bundle(CELL, props=[prop("crate-1", HELM, self.CRATE_AT)]),
            Budget.declared(CELL, {}))
        if dressed:
            self.stand(cell, wardrobe=Wardrobe("guard", {"head": HELM}))
        return cell

    def test_a_prop_and_a_worn_model_both_survive(self):
        alone = self.draw_list(self.with_prop(False)).instances[(CELL, HELM)]
        both = self.draw_list(self.with_prop(True)).instances[(CELL, HELM)]
        self.assertTrue(alone, "the crate alone packed no instance")
        self.assertEqual(len(both), 2 * len(alone),
                         "one crate and one helm should be two instances; the "
                         "guard's helm replaced the crate rather than joining "
                         "it")


class TestTheOrderIsTheRigsOrder(CharacterFixture):
    """Two backends drawing one entity have to issue the same draws in the
    same sequence, so a recorded draw list is comparable at all."""

    def test_worn_models_come_back_in_bone_order_not_dictionary_order(self):
        bones = [b.bone_id for b in humanoid_region_set().bones]
        backwards = {bone_id: HELM for bone_id in reversed(bones)}
        self.assertNotEqual(list(backwards), bones,
                            "the fixture dict is already in rig order, so "
                            "this proves nothing")

        skeleton = Skeleton("npc", humanoid_region_set(),
                            wardrobe=Wardrobe("guard", backwards))
        self.assertEqual([bone_id for bone_id, _r, _t in
                          skeleton.worn_models()], bones)
