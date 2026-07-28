"""Grouping a station's rubric into the images it needs to be sittable.

Motivating failure: a station marking eight signs across both eyes opened with
a single right-eye photograph, so seven of its eight marks could not be earned
however well the candidate answered.
"""

from __future__ import annotations

from app.services.osce.coverage import required_views, station_views


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

    assert len(station_views(_Station())) <= 4


def test_a_station_with_no_examination_task_asks_for_nothing() -> None:
    class _Station:
        prompts = [HISTORY_TASK]
        findings_elicited = None

    assert station_views(_Station()) == []
