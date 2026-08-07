"""The opening stem must not contain the answer.

Station 156 opened a real circuit with "The patient presents with bilateral
Brown's Syndrome" printed beside the visual acuity, so every diagnostic mark
was free before the candidate had looked at anything. Unlike a lost transcript,
this inflates a score rather than deflating it, which makes it far harder to
notice from the result.

The split prompt forbids it and the model did it anyway. These tests cover the
check that does not depend on the model complying.
"""

from __future__ import annotations

from app.models import OsceStation
from app.services.osce.findings import withhold_diagnosis


def test_a_sentence_naming_the_diagnosis_is_withheld():
    station = OsceStation(diagnosis="Bilateral Brown's Syndrome")
    given, moved = withhold_diagnosis(
        "Visual acuity R 6/6, L 6/6. The patient presents with bilateral "
        "Brown's Syndrome. IOP 14 mmHg.",
        station,
    )
    assert given == "Visual acuity R 6/6, L 6/6. IOP 14 mmHg."
    assert moved == ["The patient presents with bilateral Brown's Syndrome."]


def test_the_measurements_a_candidate_is_owed_are_kept():
    """The examiner really does hand over acuity and pressure. Withholding them
    would be a different way of breaking the station."""
    station = OsceStation(diagnosis="Primary open angle glaucoma")
    given, moved = withhold_diagnosis("Visual acuity R 6/6. IOP 25 mmHg.", station)
    assert given == "Visual acuity R 6/6. IOP 25 mmHg."
    assert moved == []


def test_a_conclusion_is_caught_even_when_it_is_not_the_whole_diagnosis():
    """Station 178 said "advanced glaucoma and maximally tolerated medical
    therapy" - not the diagnosis verbatim, and it gives away just as much."""
    station = OsceStation(diagnosis="Primary open angle glaucoma")
    given, moved = withhold_diagnosis(
        "Visual acuity: R 6/9, L 6/24. IOP of 25, advanced glaucoma on maximal therapy.",
        station,
    )
    assert "glaucoma" not in given.lower()
    assert moved


def test_a_generic_word_in_the_diagnosis_does_not_withhold_the_acuity():
    """"Visual acuity" must survive a diagnosis containing the word "visual",
    or the guard would empty the stem of the very thing it is for."""
    station = OsceStation(diagnosis="Cortical visual impairment")
    given, moved = withhold_diagnosis("Visual acuity R 6/60, L 6/60.", station)
    assert given == "Visual acuity R 6/60, L 6/60."
    assert moved == []


def test_a_station_with_no_diagnosis_recorded_is_left_alone():
    station = OsceStation(diagnosis=None)
    text = "Visual acuity R 6/6. IOP 14 mmHg."
    assert withhold_diagnosis(text, station) == (text, [])
