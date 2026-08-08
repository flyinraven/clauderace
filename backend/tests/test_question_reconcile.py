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

from app.services.osce.reconcile import (
    STATE,
    TRIM,
    UNCHANGED,
    classify_prompt,
    named_investigations,
)


def test_a_question_showing_everything_it_asked_for_is_left_alone():
    prompt = {
        "text": "This is his Pentacam scan of the left eye. What does it show?",
        "image_wanted": "Pentacam imaging of the left eye",
        "figure_ids": [11],
    }
    assert classify_prompt(prompt, {11: "Pentacam elevation maps of the left eye"})[0] == UNCHANGED


def test_a_question_promising_more_images_than_arrived_is_trimmed():
    """Station 133: "These are his FAF, Disc OCT, and visual fields" - two came."""
    prompt = {
        "text": "These are his FAF, Disc OCT, and visual fields. Talk me through them.",
        "image_wanted": "Fundus autofluorescence; disc OCT; Humphrey visual fields",
        "figure_ids": [21, 22, 23],
    }
    mode, here, missing = classify_prompt(
        prompt, {21: "Fundus autofluorescence", 22: "Disc OCT"}
    )
    assert mode == TRIM
    assert here == [21, 22]
    assert missing == {"visual_field"}


def test_a_question_whose_images_never_arrived_is_restated():
    """Station 176, which cost a real circuit 20%."""
    prompt = {
        "text": "This is his examination and ocular biometry data. Talk me through them.",
        "image_wanted": "Ultrasound biometry data",
        "figure_id": 31,
    }
    assert classify_prompt(prompt, {})[0] == STATE


def test_an_unapproved_figure_does_not_count_as_shown():
    """`shown` is what the candidate sees, not what the row points at. A figure
    the vision gate held back is bound to the question and invisible."""
    prompt = {
        "text": "This is her CT of the orbits. What does it show?",
        "image_wanted": "CT orbits",
        "figure_id": 41,
    }
    # 41 exists and is bound, but is not in the approved-and-attached set.
    assert classify_prompt(prompt, {})[0] == STATE


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
        assert classify_prompt({"text": text}, {})[0] == UNCHANGED, text


def test_a_question_that_states_its_own_finding_is_already_right():
    """Station 61 does this unprompted, and it is the shape `STATE` produces."""
    prompt = {
        "text": (
            "The ERG is undetectable and the visual field shows severe "
            "constriction with a small central island. What does this tell you?"
        )
    }
    assert classify_prompt(prompt, {})[0] == UNCHANGED


def test_an_impossible_result_still_counts_as_needing_a_rewrite():
    """"This is her Quantiferon Gold result" - no search will ever find one, and
    the pipeline knew: it set `image_impossible` and asked the question anyway."""
    prompt = {
        "text": "This is her Quantiferon Gold result. What does it show?",
        "image_impossible": "a result to be read, not an image",
    }
    assert classify_prompt(prompt, {})[0] == STATE


def test_the_opening_examination_question_is_not_swept_up():
    """It carries no `figure_id` because the station's opening image sits at
    position 0, outside the prompts. Treating it as imageless would rewrite the
    one question on every station that was never broken."""
    prompt = {"text": "Please examine the fundus of both eyes."}
    assert classify_prompt(prompt, {5: "Fundus photograph of both eyes"})[0] == UNCHANGED


def test_two_investigations_joined_by_and_are_two_things_to_show():
    """The miss that made this its own rule.

    `split_investigations` reads "photographs and autofluorescence" as one
    request and `expected_modalities` reads both as "fundus", so a question
    asking for both and given one photograph looked fully served. It was
    station 194, and a real circuit scored 17.5% on it.
    """
    prompt = {
        "text": "Talk me through these retinal images. What do they show?",
        "image_wanted": (
            "Bilateral wide-field colour fundus photographs and fundus autofluorescence"
        ),
        "figure_id": 60,
    }
    mode, here, missing = classify_prompt(prompt, {60: "Fundus photograph, laterality not specified."})
    assert mode == TRIM
    assert missing == {"faf"}


def test_a_ct_is_not_an_mri():
    """Station 257 asked for a CT of the orbits and showed a coronal MRI of the
    head. Both are "radiology" to the modality gate, so nothing objected."""
    prompt = {
        "text": "This is her CT scan of the orbits. What does it show?",
        "image_wanted": "CT scan of the orbits, axial and coronal views",
        "figure_id": 913,
    }
    mode, _, missing = classify_prompt(prompt, {913: "Coronal MRI of the head"})
    assert mode == TRIM
    assert missing == {"ct"}


def test_the_terms_that_must_stay_apart():
    assert named_investigations("fundus photographs and autofluorescence") == {
        "fundus_photo", "faf",
    }
    assert named_investigations("corneal topography and A-scan biometry") == {
        "topography", "biometry",
    }
    assert named_investigations("CT orbits") == {"ct"}
    assert named_investigations("MRI brain") == {"mri"}


def test_what_is_on_screen_is_the_caption_not_the_request():
    """The bug that let station 194 through a first time.

    A figure carries both what was asked for and what a vision model saw. Using
    the request to describe the screen makes the comparison answer itself: a
    figure requested as "photographs and autofluorescence" appears to show both
    however little arrived. Only the caption says what is really there.
    """
    from app.services.osce.reconcile import _shown_figures  # noqa: PLC0415

    assert _shown_figures.__doc__  # the rule is documented where it is enforced
    prompt = {
        "text": "Talk me through these retinal images.",
        "image_wanted": "Colour fundus photographs and fundus autofluorescence",
        "figure_id": 60,
    }
    # Caption describes the image; the request must not be mixed in.
    assert classify_prompt(prompt, {60: "Fundus photograph of the left eye"})[0] == TRIM
    # And when it genuinely is both, nothing is rewritten.
    assert classify_prompt(
        prompt, {60: "Colour fundus photograph and autofluorescence pair"}
    )[0] == UNCHANGED


def test_restating_a_question_keeps_what_it_asked_for():
    """Two facts, not one field.

    "Do not search for this again" and "this is what the question needed" are
    different things, and expressing the first by deleting the second cost 22
    questions the chance of being handed a figure from the examiners' own
    report: `bind_ingested_figures_to_questions` matches a question's request
    against the figures the station already holds, and a question with no
    request can never be matched.
    """
    import inspect

    from app.services.osce.reconcile import reconcile_station

    source = inspect.getsource(reconcile_station)
    assert 'prompt["image_search_exhausted"] = True' in source
    assert 'prompt.pop("image_wanted"' not in source, (
        "the request must survive so the binder can still match a paper figure"
    )


def test_a_question_already_restated_is_not_restated_again():
    """It would be rewritten on every run, and each rewrite overwrites the
    record of the one before - which is how six questions lost the request they
    were written with."""
    prompt = {
        "text": "What would you expect his chest X-ray to show?",
        "image_wanted": "Chest X-ray showing bilateral hilar lymphadenopathy",
        "reconciled": {"mode": STATE, "basis": "expected"},
    }
    assert classify_prompt(prompt, {})[0] == UNCHANGED


def test_a_restated_question_is_revisited_once_an_image_arrives():
    """The binder may yet hand it the paper's own figure, and then the wording
    should catch up."""
    prompt = {
        "text": "What would you expect his OCT to show?",
        "image_wanted": "OCT of the macula",
        "figure_id": 7,
        "reconciled": {"mode": STATE, "basis": "expected"},
    }
    from app.services.osce.reconcile import RESTORE

    # What arrived is the OCT it asked for, so the restatement goes.
    assert classify_prompt(prompt, {7: "Optical coherence tomography of one macula"})[0] == RESTORE
    # A restated question handed the WRONG picture is restored too - the stated
    # finding must come out either way - and the caller then trims the restored
    # wording down to what actually arrived, in the same pass.
    mode, _, missing = classify_prompt(prompt, {7: "Fundus photograph of the left eye"})
    assert mode == RESTORE
    assert missing == {"oct"}, "and the trim that follows knows what is short"


def test_a_restated_question_is_put_back_once_its_image_arrives():
    """Station 201, and the reason this cannot be left alone.

    It was restated to "her corneal topography shows approximately 2 dioptres
    of regular astigmatism" because no image could be found. The binder then
    found the report's own topography. With the picture displayed beside that
    sentence the candidate is shown the image and told the answer, and then
    asked to describe it - a free mark, and the opposite of what the station
    tests.
    """
    from app.services.osce.reconcile import RESTORE

    prompt = {
        "text": "Her corneal topography shows approximately 2 dioptres of regular astigmatism.",
        "image_wanted": "Bilateral corneal topography",
        "figure_id": 625,
        "image_search_exhausted": True,
        "reconciled": {
            "mode": STATE,
            "basis": "recorded",
            "original": "This is her corneal topography. Talk me through what it shows.",
        },
    }
    mode, here, _ = classify_prompt(prompt, {625: "Corneal topography of the right eye"})
    assert mode == RESTORE
    assert here == [625]


def test_a_restated_question_with_still_nothing_shown_is_left_as_it_is():
    prompt = {
        "text": "What would you expect her chest X-ray to show?",
        "image_wanted": "Chest X-ray",
        "image_search_exhausted": True,
        "reconciled": {"mode": STATE, "basis": "expected", "original": "This is her chest X-ray."},
    }
    assert classify_prompt(prompt, {})[0] == UNCHANGED


def test_restoring_and_trimming_are_not_alternatives():
    """Station 201, and what a first attempt got wrong.

    It named topography AND pachymetry and only the topography bound, so
    `missing` was not empty and it was trimmed instead of restored. A trim
    rewrites the clause naming the images and leaves the stated finding exactly
    where it is - so the candidate still saw the topography beside "shows
    approximately 2 dioptres of regular astigmatism". The statement has to come
    out first; the over-promise is then an ordinary trim.
    """
    from app.services.osce.reconcile import RESTORE

    prompt = {
        "text": "Her corneal topography shows approximately 2 dioptres of regular astigmatism.",
        "image_wanted": "Bilateral corneal topography with pachymetry",
        "figure_id": 625,
        "reconciled": {
            "mode": STATE,
            "original": "This is her corneal topography and pachymetry. Talk me through them.",
        },
    }
    mode, here, missing = classify_prompt(prompt, {625: "Corneal topography of the right eye"})
    assert mode == RESTORE, "a partial match must still lose the stated finding"
    assert missing == {"pachymetry"}, "and the trim that follows knows what is short"


def test_a_second_rewrite_does_not_overwrite_the_first_original():
    """Station 201 lost its true wording this way.

    A rewrite stored `original` as the text it was given - which on a second
    pass is the text the first pass wrote. So the sentence that stated the
    station's findings became the "original", and the restore meant to remove
    that statement had nothing to restore to. Six questions lost their image
    request identically.
    """
    import inspect

    from app.services.osce.reconcile import reconcile_station

    source = inspect.getsource(reconcile_station)
    assert 'previous.get("original") or prompt.get("text")' in source
    assert 'previous.get("original_image_wanted")' in source
