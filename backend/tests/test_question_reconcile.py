"""Questions must match the images that actually arrived.

The motivating circuit: 7 Aug 2026. "This is his examination and ocular
biometry data. Talk me through what they show" with an empty screen, and "Talk
me through these retinal images" - plural, both eyes, with autofluorescence -
showing one photograph of one eye. Marks were apportioned to findings never
displayed, so the score understated the candidate.

`classify_prompt` is pure, so the rule can be checked here without a model.
That matters: the rewrite is a judgement call, but *which* questions get
rewritten is not, and it is the part that must never drift.
"""

from __future__ import annotations

from app.services.osce.reconcile import STATE, TRIM, UNCHANGED, classify_prompt


def test_a_question_showing_everything_it_asked_for_is_left_alone():
    prompt = {
        "text": "This is his Pentacam scan of the left eye. What does it show?",
        "image_wanted": "Pentacam imaging of the left eye",
        "figure_ids": [11],
    }
    assert classify_prompt(prompt, shown={11})[0] == UNCHANGED


def test_a_question_promising_more_images_than_arrived_is_trimmed():
    """Station 133: "These are his FAF, Disc OCT, and visual fields" - two came."""
    prompt = {
        "text": "These are his FAF, Disc OCT, and visual fields. Talk me through them.",
        "image_wanted": "Fundus autofluorescence; disc OCT; Humphrey visual fields",
        "figure_ids": [21, 22, 23],
    }
    mode, here, asked = classify_prompt(prompt, shown={21, 22})
    assert mode == TRIM
    assert here == [21, 22]
    assert asked == 3


def test_a_question_whose_images_never_arrived_is_restated():
    """Station 176, which cost a real circuit 20%."""
    prompt = {
        "text": "This is his examination and ocular biometry data. Talk me through them.",
        "image_wanted": "Ultrasound biometry data",
        "figure_id": 31,
    }
    assert classify_prompt(prompt, shown=set())[0] == STATE


def test_an_unapproved_figure_does_not_count_as_shown():
    """`shown` is what the candidate sees, not what the row points at. A figure
    the vision gate held back is bound to the question and invisible."""
    prompt = {
        "text": "This is her CT of the orbits. What does it show?",
        "image_wanted": "CT orbits",
        "figure_id": 41,
    }
    # 41 exists and is bound, but is not in the approved-and-attached set.
    assert classify_prompt(prompt, shown=set())[0] == STATE


def test_a_question_that_never_wanted_an_image_is_never_touched():
    """"What would you look for on corneal topography in this patient?" is a
    hypothetical. Twelve questions in the bank read like this and all of them
    are fine; rewriting them would be the damage, not the fix."""
    for text in (
        "What would you look for on corneal topography in this patient?",
        "What would you expect fluorescein angiography to add?",
        "What are the driving licence requirements for visual field defects?",
        "How would you perform biometry in this young child?",
    ):
        assert classify_prompt({"text": text}, shown=set())[0] == UNCHANGED, text


def test_a_question_that_states_its_own_finding_is_already_right():
    """Station 61 does this unprompted, and it is the shape `STATE` produces."""
    prompt = {
        "text": (
            "The ERG is undetectable and the visual field shows severe "
            "constriction with a small central island. What does this tell you?"
        )
    }
    assert classify_prompt(prompt, shown=set())[0] == UNCHANGED


def test_an_impossible_result_still_counts_as_needing_a_rewrite():
    """"This is her Quantiferon Gold result" - no search will ever find one, and
    the pipeline knew: it set `image_impossible` and asked the question anyway."""
    prompt = {
        "text": "This is her Quantiferon Gold result. What does it show?",
        "image_impossible": "a result to be read, not an image",
    }
    assert classify_prompt(prompt, shown=set())[0] == STATE


def test_the_opening_examination_question_is_not_swept_up():
    """It carries no `figure_id` because the station's opening image sits at
    position 0, outside the prompts. Treating it as imageless would rewrite the
    one question on every station that was never broken."""
    prompt = {"text": "Please examine the fundus of both eyes."}
    assert classify_prompt(prompt, shown={5})[0] == UNCHANGED
