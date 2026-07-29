"""The gate that keeps the wrong kind of image off a question.

Motivating failure: a station whose first task was "examine the anterior
segment of both eyes" opened with a fluorescein angiogram of the fundus. The
vision grader had passed it, because the station's findings did mention the
disc — it was answering the wrong question.
"""

from __future__ import annotations

from app.services.imagesearch.relevance import expected_modalities, modality_mismatch
from app.services.osce.station_images import expected_modalities_for


class _Station:
    def __init__(self, prompts=None, tasks=None):
        self.prompts = prompts or []
        self.tasks = tasks or []


def test_anterior_segment_task_refuses_an_angiogram() -> None:
    expected = expected_modalities("Please examine the anterior segment of both eyes.")

    assert "slit_lamp" in expected
    assert modality_mismatch(expected, "angiogram") is not None
    assert modality_mismatch(expected, "fundus") is not None
    assert modality_mismatch(expected, "slit_lamp") is None
    assert modality_mismatch(expected, "external") is None


def test_the_station_opening_image_is_governed_by_the_first_task() -> None:
    station = _Station(prompts=[
        {"label": "A", "text": "Please examine the anterior segment of both eyes."},
        {"label": "B", "text": "Describe the fundus findings."},
    ])
    expected = expected_modalities_for(station, wanted=None)

    # The angiogram that actually shipped must not clear the gate.
    assert modality_mismatch(expected, "angiogram") is not None


def test_a_question_asking_for_a_named_modality_gets_that_modality() -> None:
    expected = expected_modalities("OCT of the right macula showing intraretinal fluid")

    assert expected == frozenset({"oct"})
    assert modality_mismatch(expected, "fundus") is not None
    assert modality_mismatch(expected, "oct") is None


def test_a_figure_requested_by_a_question_overrides_the_station() -> None:
    station = _Station(prompts=[{"label": "A", "text": "Examine the anterior segment."}])
    expected = expected_modalities_for(station, wanted="MRI of the orbits")

    assert expected == frozenset({"radiology"})
    assert modality_mismatch(expected, "radiology") is None


def test_posterior_task_accepts_the_several_ways_of_imaging_it() -> None:
    expected = expected_modalities("Examine the fundus of the left eye.")

    for modality in ("fundus", "angiogram", "oct", "ultrasound"):
        assert modality_mismatch(expected, modality) is None
    assert modality_mismatch(expected, "slit_lamp") is not None


def test_wording_that_names_nothing_does_not_gate() -> None:
    """A filter that guesses would silently discard good images."""
    expected = expected_modalities("Describe what you see and give a differential.")

    assert expected == frozenset()
    assert modality_mismatch(expected, "angiogram") is None


def test_an_unknown_modality_answer_does_not_gate() -> None:
    expected = expected_modalities("Examine the anterior segment.")

    for answer in (None, "", "other", "unknown", "something the model made up"):
        assert modality_mismatch(expected, answer) is None


# --- The last-resort description -----------------------------------------
# Some signs have no photograph at all. Fatiguable ptosis and Cogan's lid
# twitch are manoeuvres over time; a thymectomy scar is on the chest. The
# station states them instead, and must not name the diagnosis while doing it.

from app.services.osce.station_images import leaked_term  # noqa: E402


class _DiagnosedStation:
    def __init__(self, diagnosis: str | None, case_summary: str | None = None):
        self.diagnosis = diagnosis
        self.case_summary = case_summary
        self.id = 1


def test_a_description_naming_the_diagnosis_is_refused() -> None:
    station = _DiagnosedStation("Myasthenia gravis with ocular involvement")
    assert leaked_term("There is bilateral ptosis due to myasthenia gravis.", station)


def test_naming_only_part_of_it_still_leaks() -> None:
    station = _DiagnosedStation("Myasthenia gravis with ocular involvement")
    assert leaked_term("The findings are those of ocular myasthenia.", station)


def test_describing_the_signs_alone_passes() -> None:
    station = _DiagnosedStation("Myasthenia gravis with ocular involvement")
    text = (
        "There is bilateral asymmetric ptosis, worse on the left. Ptosis of the "
        "right lid increases when the left lid is held up, and a brief upward "
        "overshoot of the lid is seen on refixation from downgaze."
    )
    assert not leaked_term(text, station)


def test_common_words_in_a_diagnosis_do_not_trip_the_guard() -> None:
    """"Left", "eye" and "syndrome" appear in half the descriptions written."""
    station = _DiagnosedStation("Chronic left ocular syndrome")
    assert not leaked_term("There is ptosis of the left eye.", station)


def test_a_station_with_no_recorded_diagnosis_has_nothing_to_leak() -> None:
    assert not leaked_term("Any description at all.", _DiagnosedStation(None))


def test_characterising_the_sign_leaks_even_without_naming_it() -> None:
    """The failure that made this more than a word check.

    A visual field station was told the defect was "congruous" and that the
    macula was "spared". Neither word appears in the diagnosis, and together
    they are the entire answer to the question being asked.
    """
    station = _DiagnosedStation("Left homonymous hemianopia due to occipital infarct")
    assert leaked_term(
        "The patient has a congruous visual field defect. The macula appears to be spared.",
        station,
    )


def test_reporting_the_same_field_defect_plainly_is_allowed() -> None:
    station = _DiagnosedStation("Left homonymous hemianopia due to occipital infarct")
    assert not leaked_term(
        "There is reduced sensitivity in the left half of each field. The optic discs are normal.",
        station,
    )


def test_drawing_any_conclusion_is_refused() -> None:
    station = _DiagnosedStation("Something unrelated")
    for phrase in (
        "Findings are consistent with an anterior lesion.",
        "The appearance is typical of this condition.",
        "This is pathognomonic.",
        "The features are suggestive of inflammation.",
    ):
        assert leaked_term(phrase, station), phrase
