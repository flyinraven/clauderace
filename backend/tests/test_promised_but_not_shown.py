"""A question must not hand over something the station is not showing.

This is the fault that survived every other check: the pipeline was correct at
each stage, and no stage compared the words of a question against the captions
of the figures beside it. Station 605 said "here is a slit lamp view of the
patient's right eye" over a fundus photograph, 604 asked for an OCT above two
fundus photographs, and 656 promised the fundus photographs of both eyes with
no figure at all. All three reported themselves sittable, and each was found by
a person reading one station at a time.
"""

from __future__ import annotations

from app.constants import SOURCE_PAST_PAPER
from app.models import OsceFigure, OsceStation
from app.services.osce.sittability import station_faults


def _station(question: str, figures=()) -> OsceStation:
    station = OsceStation(
        source=SOURCE_PAST_PAPER,
        subspecialty="Cataract",
        prompts=[{"label": "A", "text": question, "rubric": []}],
    )
    station.figures = list(figures)
    return station


def _image(caption: str) -> OsceFigure:
    return OsceFigure(image_id=1, caption=caption, is_approved=True,
                      verification_status="from_paper", position=1)


def _described(text: str) -> OsceFigure:
    return OsceFigure(image_id=None, described_findings=text,
                      described_findings_approved=True, position=1)


def _kinds(station: OsceStation) -> list[str]:
    return [f.kind for f in station_faults(station)]


def test_a_slit_lamp_question_over_a_fundus_photograph_is_flagged() -> None:
    """Station 605, exactly."""
    station = _station(
        "Here is a slit lamp view of the patient's right eye. Describe what you see.",
        [_image("Fundus photograph of the right eye")],
    )

    assert "promises_what_is_not_shown" in _kinds(station)


def test_an_oct_question_over_fundus_photographs_is_flagged() -> None:
    """Station 604. A macular hole is diagnosed on the OCT, not the photograph."""
    station = _station(
        "Here is an OCT of the patient's left eye. Describe what it shows.",
        [_image("Fundus photograph"), _image("Fundus photograph")],
    )

    assert "promises_what_is_not_shown" in _kinds(station)


def test_a_question_promising_photographs_with_no_figures_is_flagged() -> None:
    """Station 656 promised both eyes' fundus photographs and showed nothing."""
    station = _station("Here are the fundus photographs of both eyes. Describe them.")

    assert "promises_what_is_not_shown" in _kinds(station)


def test_the_modality_the_station_shows_is_not_flagged() -> None:
    station = _station(
        "Here is an OCT of the left macula. Describe what it shows.",
        [_image("Optical coherence tomography of the left macula")],
    )

    assert "promises_what_is_not_shown" not in _kinds(station)


def test_findings_stated_in_words_count_as_shown() -> None:
    """Sourcing fails often and the findings are then stated instead. A station
    that has been through that is settled, and must not be sent back."""
    station = _station(
        "Here are the findings of this patient's OCT. Describe what they show.",
        [_described("The OCT shows a full thickness macular hole with an operculum.")],
    )

    assert "promises_what_is_not_shown" not in _kinds(station)


def test_naming_a_finding_without_handing_anything_over_is_not_flagged() -> None:
    """Station 691: the diagnosis mentions field defects; no chart is claimed.
    Flagging this spent a search on a station that was already correct."""
    station = _station(
        "The diagnosis is craniopharyngioma with optic atrophy, pituitary failure "
        "and visual field defects. How would you manage him?",
        [_image("Fundus photograph of the right eye")],
    )

    assert "promises_what_is_not_shown" not in _kinds(station)
