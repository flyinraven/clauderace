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


# --- Grounding: a description may only state the station's own findings ---
# Told the recorded findings were the only facts it could use, the model still
# described a retracted upper lid and a forward-displaced globe for a station
# whose findings are a cicatricial ectropion of the LOWER lids. Instruction was
# not enough, so the result is checked.

from app.services.osce.station_images import grounding_problem  # noqa: E402


class _StationWithFindings:
    def __init__(self, findings: str | None, subspecialty: str = "Oculoplastics & Orbit"):
        self.findings_elicited = findings
        self.findings = None
        self.subspecialty = subspecialty
        self.diagnosis = None
        self.case_summary = None
        self.id = 1


ECTROPION = _StationWithFindings(
    "The patient has bilateral lower lid cicatricial ectropion with anterior "
    "lamellar shortening, mild horizontal lid laxity, and tarsal thickening."
)


def test_a_description_of_a_different_condition_is_refused() -> None:
    """The exact sentence the live run produced."""
    problem = grounding_problem(
        "The patient's right upper eyelid is retracted, and the globe is displaced forwards.",
        ECTROPION,
        None,
    )
    assert problem
    assert "retracted" in problem


def test_plain_words_for_the_recorded_sign_are_allowed() -> None:
    """The diagnosis IS the sign here, so a faithful description must avoid
    naming it and reach for ordinary words instead."""
    assert grounding_problem(
        "Both lower lids are turned outwards away from the globe. The lower lids "
        "feel slightly loose when pulled.",
        ECTROPION,
        None,
    ) is None


def test_the_verb_form_of_a_recorded_finding_is_not_an_invention() -> None:
    """A suffix stemmer made "elevation" and "elevates" different words."""
    station = _StationWithFindings(
        "There is limited elevation of the right eye. Cover test shows a large "
        "right hypotropia measuring 50 prism dioptres.",
        "Ocular Motility",
    )
    assert grounding_problem(
        "The right eye elevates poorly. On cover testing the right eye sits lower, "
        "measuring 50 prism dioptres.",
        station,
        None,
    ) is None


def test_signs_from_another_station_entirely_are_refused() -> None:
    assert grounding_problem(
        "There is a dense cataract and the optic disc is pale.", ECTROPION, None
    )


def test_a_station_with_no_findings_cannot_be_described() -> None:
    assert grounding_problem("Anything at all.", _StationWithFindings(None), None)


def test_anatomy_in_the_diagnosis_is_not_a_forbidden_word() -> None:
    """The log line that explained why no description was ever produced.

    Station 87's diagnosis is "Adie's pupil", so the guard treated "pupil" as
    giving the answer away - and a dilated pupil with light-near dissociation
    cannot be described without it. Every attempt was discarded. The same trap
    sits under optic disc drusen, band keratopathy and macular hole.
    """
    station = _DiagnosedStation("Adie's pupil")
    assert leaked_term(
        "The left pupil is larger than the right and constricts poorly to light.",
        station,
    ) is None
    assert leaked_term("This is an Adie's pupil.", station), "the name still leaks"


def test_a_plain_rendering_of_the_findings_survives_both_checks() -> None:
    """Together the guards must still let a good description through."""
    class _Station:
        diagnosis = "Adie's pupil"
        case_summary = None
        subspecialty = "Neuro-ophthalmology"
        findings_elicited = (
            "Examination reveals a left dilated pupil with light-near dissociation, "
            "where the difference is greater in light than in dark. Eye movements "
            "are normal, and there is no ptosis."
        )
        findings = None
        id = 87

    station = _Station()
    text = (
        "The left pupil is larger than the right. It constricts poorly to light "
        "but briskly to a near target."
    )
    assert leaked_term(text, station) is None
    assert grounding_problem(text, station, None) is None


def test_grounding_reports_a_concern_rather_than_deciding() -> None:
    """It cannot tell paraphrase from invention, so it must not have the vote.

    Three consecutive live runs discarded a correct description for ordinary
    examination words the findings happened not to contain - "larger",
    "constricts", then "convergence" for how a near response is tested. The
    reviewer is told what to look at; the description still reaches them.
    """
    station = _DiagnosedStation("Something unrelated")
    station.findings_elicited = "There is a left dilated pupil with light-near dissociation."
    station.findings = None
    station.subspecialty = "Neuro-ophthalmology"

    problem = grounding_problem(
        "The near response is tested by convergence, which is better than to bright light.",
        station,
        None,
    )
    assert problem, "the reviewer should still be told"
    assert "convergence" in problem
