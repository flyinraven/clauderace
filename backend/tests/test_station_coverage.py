"""Grouping a station's rubric into the images it needs to be sittable.

Motivating failure: a station marking eight signs across both eyes opened with
a single right-eye photograph, so seven of its eight marks could not be earned
however well the candidate answered.
"""

from __future__ import annotations

from app.services.osce.coverage import MAX_VIEWS, required_views, station_views


ANTERIOR_TASK = {
    "label": "A",
    "text": "Please examine the anterior segment of both eyes.",
    "rubric": [
        {"text": "Identify and describe microcornea in the right eye."},
        {"text": "Identify and describe nystagmus in the right eye."},
        {"text": "Identify and describe a sulcus IOL in the right eye."},
        {"text": "Identify and describe polycoria in the right eye."},
        {"text": "Identify and describe a Baerveldt tube in the right eye."},
        {"text": "Identify and describe a recent penetrating keratoplasty (PK) in the left eye."},
        {"text": "Identify and describe aniridia in the left eye."},
        {"text": "Identify and describe a Baerveldt tube in the left eye."},
    ],
}

HISTORY_TASK = {
    "label": "B",
    "text": "What other history would you want to elicit in a patient with congenital cataracts?",
    "rubric": [
        {"text": "Asks about maternal illness in pregnancy."},
        {"text": "Asks about a family history of cataract."},
    ],
}


def test_a_two_eyed_task_needs_one_view_per_eye() -> None:
    views = required_views(ANTERIOR_TASK)

    assert [v.laterality for v in views] == ["left", "right"]


def test_each_view_carries_its_own_eye_s_findings() -> None:
    views = {v.laterality: v for v in required_views(ANTERIOR_TASK)}

    right = views["right"].wanted_description
    assert "polycoria" in right
    assert "Baerveldt tube" in right
    assert "right eye" in right
    # The left eye's signs must not leak into the right eye's search.
    assert "aniridia" not in right
    assert "keratoplasty" not in right

    left = views["left"].wanted_description
    assert "aniridia" in left
    assert "polycoria" not in left


def test_the_marker_s_instruction_is_stripped_from_the_search() -> None:
    """Searching "identify and describe polycoria" returns teaching slides."""
    right = {v.laterality: v for v in required_views(ANTERIOR_TASK)}["right"]

    assert not right.wanted_description.lower().startswith("identify")
    assert "identify and describe" not in right.wanted_description.lower()


def test_a_history_question_needs_no_image() -> None:
    assert required_views(HISTORY_TASK) == []


def test_views_are_capped_so_a_station_is_not_padded() -> None:
    class _Station:
        prompts = [ANTERIOR_TASK, dict(ANTERIOR_TASK, label="C"), dict(ANTERIOR_TASK, label="D")]
        findings_elicited = None

    # Against the constant, not a literal: the cap moved when views began
    # splitting by examination as well as by eye, and the rule being tested
    # is that there is a cap at all.
    assert len(station_views(_Station())) <= MAX_VIEWS


def test_a_station_with_no_examination_task_asks_for_nothing() -> None:
    class _Station:
        prompts = [HISTORY_TASK]
        findings_elicited = None

    assert station_views(_Station()) == []


# --- Splitting by examination, not just by eye ----------------------------
# Grouping by eye alone put "the OCT shows cystoid macular oedema" and "the
# lens is subluxed" in one view. Whichever image was sourced, the other point
# was unearnable - the station looked covered and could not be answered.

OCT_AND_LENS_TASK = {
    "label": "A",
    "text": "Please examine the anterior segment and describe what you see.",
    "rubric": [
        {"text": "Identify the subluxed lens in the right eye"},
        {"text": "Note the OCT shows cystoid macular oedema in the right eye"},
        {"text": "Comment on the raised intraocular pressure"},
    ],
}


def test_a_named_investigation_gets_a_view_of_its_own() -> None:
    views = required_views(OCT_AND_LENS_TASK)
    modalities = {v.modality for v in views}
    assert "oct" in modalities, "the OCT point needs its own image"
    assert None in modalities, "the lens point cannot be described from an OCT"


def test_a_point_naming_no_examination_is_not_folded_into_the_investigation() -> None:
    """The failure this split exists to prevent, pinned directly."""
    views = required_views(OCT_AND_LENS_TASK)
    oct_view = next(v for v in views if v.modality == "oct")
    assert not any("subluxed lens" in p.lower() for p in oct_view.points), (
        "a subluxed lens is not visible on an OCT, so listing it as covered "
        "by the OCT leaves those marks unearnable"
    )


def test_the_view_names_the_examination_it_wants() -> None:
    oct_view = next(v for v in required_views(OCT_AND_LENS_TASK) if v.modality == "oct")
    wanted = oct_view.wanted_description.lower()
    assert wanted.startswith("oct showing"), wanted
    assert "the oct shows" not in wanted, "the modality was named twice"


def test_a_general_sign_still_rides_with_every_eye() -> None:
    """Splitting by examination must not undo the laterality sharing."""
    task = {
        "text": "Examine both eyes and describe the findings.",
        "rubric": [
            {"text": "Identify the corneal scar in the right eye"},
            {"text": "Identify the corneal scar in the left eye"},
            {"text": "Note the bilateral conjunctival injection"},
        ],
    }
    views = required_views(task)
    assert len(views) == 2
    for view in views:
        assert any("injection" in p.lower() for p in view.points)


def test_an_investigation_reported_as_normal_needs_no_image() -> None:
    """Found on a real station: it wanted an OCT of a normal eye, twice.

    "Correlates findings with the given acuity, refraction and normal
    fundal/OCT report" names an OCT, but only to say it was normal. Splitting
    on the mention sourced an image of nothing, on both eyes.
    """
    task = {
        "text": "Examine the lens and describe what you see.",
        "rubric": [
            {"text": "Identify the dense posterior polar plaque in the right eye"},
            {"text": "Correlates findings with the given acuity and normal fundal/OCT report"},
        ],
    }
    views = required_views(task)
    assert all(v.modality != "oct" for v in views), [v.modality for v in views]
    assert len(views) == 1


def test_a_reasoning_point_never_drives_a_search() -> None:
    task = {
        "text": "Describe the findings.",
        "rubric": [
            {"text": "Interprets the visual field as a bitemporal hemianopia"},
            {"text": "Summarises the case"},
        ],
    }
    assert required_views(task) == []


def test_an_abnormal_investigation_still_gets_its_own_view() -> None:
    """The guard must not swallow the investigations that do need showing."""
    task = {
        "text": "Examine the disc and describe what you see.",
        "rubric": [
            {"text": "Identify the swollen optic disc"},
            {"text": "Note the OCT shows subretinal fluid at the macula"},
        ],
    }
    assert {v.modality for v in required_views(task)} == {None, "oct"}


def test_a_management_proposal_is_not_something_to_photograph() -> None:
    """"Proposes a test to assess fatiguability" wanted a picture of an ice pack."""
    task = {
        "text": "Describe your findings.",
        "rubric": [
            {"text": "Proposes a test to assess fatiguability (e.g., ice pack test)"},
            {"text": "Lists other treatment options"},
            {"text": "Include infective causes in the differential diagnoses"},
            {"text": "Organise appropriate investigations"},
        ],
    }
    assert required_views(task) == []


def test_a_point_marked_on_absence_is_not_sourced() -> None:
    """No search returns a scan showing no bony destruction."""
    task = {
        "text": "Describe the findings.",
        "rubric": [
            {"text": "Identifies right inferior globe dystopia"},
            {"text": "Important negative findings: no bony destruction on MRI"},
        ],
    }
    assert all(v.modality != "radiology" for v in required_views(task))


def test_the_instruction_strip_does_not_eat_the_word_it_is_stripping() -> None:
    """[\s,and]* matched the letters a, n and d, so "describe" lost its d."""
    task = {
        "text": "Examine the orbit and describe what you see.",
        "rubric": [{"text": "Identifies and describe globe dystopia in the right orbit"}],
    }
    wanted = required_views(task)[0].wanted_description
    assert "escribe" not in wanted, wanted
    assert wanted.lower().startswith("globe dystopia"), wanted
