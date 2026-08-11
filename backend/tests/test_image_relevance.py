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


class _ExaminedStation:
    """A station whose examiners printed their findings, as every real one does."""

    def __init__(self, diagnosis: str, findings: str):
        self.diagnosis = diagnosis
        self.findings_elicited = findings
        self.findings = None
        self.case_summary = None
        self.id = 1


def test_the_sign_the_examiners_printed_may_be_stated() -> None:
    """The failure that left 37 of 38 figures with no words at all.

    Every one of these is a word the station's own findings use, and none of
    them can be worked around: an eye that does not elevate, a disc that is
    cupped, a lid that droops. Refusing them refused the whole description,
    and the candidate met a station with no image and nothing written either.
    """
    cases = [
        ("Right monocular elevation deficiency with hypotropia and ptosis",
         "There is a right hypotropia in primary position with ptosis, and "
         "elevation of the right eye is limited.",
         "The right eye sits lower and does not elevate past the midline. "
         "There is a droop of the right upper lid."),
        ("Traumatic right aphakia with glaucoma and advanced optic disc cupping",
         "The right eye is aphakic with advanced cupping of the optic disc.",
         "The right disc is deeply cupped and no lens is present behind the pupil."),
        ("Fuchs endothelial corneal dystrophy - right eye status post DMEK",
         "Corneal guttata with stromal thickening in the left eye; the right "
         "cornea is clear following graft.",
         "The left cornea shows guttata with stromal thickening. The right "
         "cornea is clear and the graft looks stable."),
    ]
    for diagnosis, findings, description in cases:
        station = _ExaminedStation(diagnosis, findings)
        assert leaked_term(description, station) is None, (
            f"{diagnosis!r} refused a description made of its own findings"
        )


def test_the_words_of_the_findings_still_may_not_be_assembled_into_the_name() -> None:
    """Grounded words, put back together, are the answer again.

    Station 119's findings say "partially accommodative esotropia" in so many
    words, so each of them is fair to state. Saying them next to each other is
    handing the candidate what the station exists to ask.
    """
    station = _ExaminedStation(
        "Partially accommodative esotropia with bilateral inferior oblique overaction.",
        "The patient has a partially accommodative esotropia. There is "
        "bilateral inferior oblique overaction.",
    )
    assert leaked_term("There is an accommodative esotropia.", station)
    assert leaked_term("There is overaction of the inferior oblique muscles.", station), (
        "the same phrase reordered is the same phrase"
    )
    assert leaked_term(
        "The eyes show an esotropia which is accommodative in nature.", station
    )
    # The sign itself, without the qualifier that names the condition.
    assert leaked_term(
        "The right eye turns inwards, and the deviation reduces when the "
        "glasses are worn.", station
    ) is None


def test_a_name_the_findings_never_use_is_still_refused() -> None:
    """The label lives in the diagnosis alone; that is what makes it the label."""
    station = _ExaminedStation(
        "Myasthenia gravis with ocular involvement",
        "Bilateral asymmetric ptosis, worse on the left, fatiguing on sustained upgaze.",
    )
    assert leaked_term("The findings are those of ocular myasthenia.", station)
    assert leaked_term("There is bilateral ptosis due to myasthenia gravis.", station)
    assert leaked_term(
        "There is a droop of both upper lids which worsens on sustained upgaze.",
        station,
    ) is None


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


def test_the_montage_phrasing_leads_the_search_for_a_motility_station() -> None:
    """Station 7 attached from the broad phrase, which had dropped the gaze wording.

    "multiple cranial nerve palsies" returned a face in primary position, and
    the three phrasings the model wrote had all been tried first. The words a
    montage is filed under are fixed and free to write.
    """
    from app.services.osce.station_images import _gaze_first

    station = _DiagnosedStation("Right third nerve palsy from a carotid aneurysm")
    station.subspecialty = "Neuro-ophthalmology"
    queries = _gaze_first(
        ["multiple cranial nerve palsies"],
        "nine positions of gaze showing deficits in right MR, SR, IR",
        station,
    )
    assert "nine positions of gaze" in queries[0]
    assert queries[-1] == "multiple cranial nerve palsies", "the broad phrase is kept last"


def test_a_station_about_nothing_moving_keeps_its_own_queries() -> None:
    from app.services.osce.station_images import _gaze_first

    station = _DiagnosedStation("Hypermature cataract")
    station.subspecialty = "Cataract"
    assert _gaze_first(["slit lamp photograph of a hypermature cataract"], "dense white lens", station) == [
        "slit lamp photograph of a hypermature cataract"
    ]


def test_the_stations_findings_are_not_written_under_a_photograph(db):
    """Station 1A of 2020 Semester 2 carried "Histology revealed melanoma in
    situ" beneath all five of its figures - the external photograph, the
    ultrasound, and a blank image included.

    The verbatim floor states the WHOLE station. That is right when the
    examiner is standing in for an image nobody could find, and wrong under a
    picture: it is not a description of that picture, and on an opening figure
    it is the answer.

    Those words pass the leak guard honestly, because the paper records the
    histology as a finding, so every word of it is grounded in the findings.
    The guard is not the thing that should have stopped it.
    """
    from app.models import Image, OsceFigure
    from app.services.osce.station_images.describe import (
        JOB_DESCRIBE_STATION_FIGURES,
        handle_describe_station_figures,
    )
    from app.services.jobs.runner import JobContext
    from app.models import Job
    from app.models.ops import JOB_PENDING
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.figures.clear()
    station.diagnosis = "Conjunctival Melanoma (pT1b)"
    station.findings = (
        "Histology revealed melanoma in situ with atypical epithelioid cells."
    )
    station.findings_elicited = station.findings
    image = Image(sha256="e" * 64, size_bytes=5, content_type="image/jpeg",
                  data=b"x", origin="pdf")
    db.add(image)
    db.commit()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        verification_status="from_paper", is_approved=True,
                        match_confidence=0.9, verification_notes="an eye")
    db.add(figure)
    db.commit()

    job = Job(job_type=JOB_DESCRIBE_STATION_FIGURES, status=JOB_PENDING,
              payload={"figure_ids": [figure.id]}, cursor={}, total_steps=1)
    db.add(job)
    db.commit()

    class _Silent:
        """The model declines to describe, which is what sends it to the floor."""

    import app.services.osce.station_images.describe as mod
    original = mod.describe_findings
    mod.describe_findings = lambda *a, **k: (None, None)
    try:
        handle_describe_station_figures(JobContext(db=db, job=job))
    finally:
        mod.describe_findings = original

    db.refresh(figure)
    assert not (figure.described_findings or "").strip(), (
        "a picture with no words beats the station's answer written under it"
    )
    assert figure.verification_status == "from_paper", "and the tier is untouched"


def _station_for_strict(db, diagnosis: str, findings: str):
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.diagnosis = diagnosis
    station.findings = findings
    station.findings_elicited = findings
    db.commit()
    return station


def test_an_opening_picture_may_not_say_the_diagnosis_even_when_grounded(db):
    """Station 3B of 2020 Semester 2 opened with "A dislocated PMMA IOL is
    present" against a diagnosis of "Dislocated IOL".

    Grounded, accurate, and the entire answer. `leaked_term` forgives it
    because the findings say it too - which is the right call where the words
    are all the candidate has, and the wrong one under a photograph.
    """
    from app.services.osce.station_images.verify import (
        leaked_term,
        names_the_diagnosis,
    )

    station = _station_for_strict(
        db, "Dislocated IOL.", "A dislocated PMMA IOL is present in the right eye."
    )
    words = "The anterior chamber is quiet. A dislocated PMMA IOL is present."

    assert leaked_term(words, station) is None, "the lenient guard lets it through"
    assert names_the_diagnosis(words, station) == "dislocated"


def test_the_strict_rule_still_lets_a_plain_sign_through(db):
    """It must not become the rule that binned 37 descriptions of 38."""
    from app.services.osce.station_images.verify import names_the_diagnosis

    station = _station_for_strict(
        db, "Leber's Hereditary Optic Neuropathy (LHON).",
        "There is a right relative afferent pupillary defect.",
    )
    words = (
        "There is a right relative afferent pupillary defect. The right red "
        "saturation is 60% and the left is 100%."
    )

    assert names_the_diagnosis(words, station) is None


def test_the_strict_rule_catches_an_acronym(db):
    from app.services.osce.station_images.verify import names_the_diagnosis

    station = _station_for_strict(
        db, "Leber's Hereditary Optic Neuropathy (LHON).", "Disc swelling.",
    )

    assert names_the_diagnosis("The disc is swollen in LHON.", station) == "lhon"


def test_a_figure_a_question_owns_is_judged_leniently(db):
    """By then the examiner has asked, and naming what a pathology slide shows
    is the point of showing it."""
    from app.models import Image, OsceFigure
    from app.services.osce.station_images.describe import _opens_the_station

    station = _station_for_strict(db, "Conjunctival Melanoma", "Pigmented lesion.")
    station.figures.clear()
    image = Image(sha256="f" * 64, size_bytes=5, content_type="image/jpeg",
                  data=b"x", origin="pdf")
    db.add(image)
    db.commit()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id)
    db.add(figure)
    db.commit()

    assert _opens_the_station(station, figure)

    station.prompts = [{"label": "C", "text": "What does this show?",
                        "figure_id": figure.id, "seconds": 120, "rubric": []}]
    db.commit()

    assert not _opens_the_station(station, figure)
