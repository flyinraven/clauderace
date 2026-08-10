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


# --- The background block --------------------------------------------------
# A real station opens by handing over a background: who the patient is, what
# brought them in, the acuity and the pressure. Ours handed over the findings
# block or nothing, and a candidate asked "can I have the VA and IOP please?"
# into a station with no way to answer.


class _SplitClient:
    def __init__(self, payload):
        self.payload = payload
        self.user = ""

    def complete_json(self, **kwargs):
        self.user = kwargs.get("user", "")
        return self.payload


def test_a_station_with_no_findings_still_gets_its_background(db):
    """Station 123 records the acuity in its case summary and nowhere else.

    Bailing on empty findings left 24 stations opening on nothing at all, while
    the number the candidate needed sat in the record the whole time.
    """
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.findings = None
    station.case_summary = (
        "A 32-year-old woman with bilateral keratoconus. Her visual acuity is "
        "6/60 in the right eye and 6/7.5 in the left eye."
    )
    station.diagnosis = "Keratoconus with left penetrating keratoplasty"
    db.commit()

    client = _SplitClient({
        "given": "32 year old woman. Visual acuity 6/60 right, 6/7.5 left.",
        "elicited": "",
    })
    split_findings(db, client, station)

    assert "6/60" in (station.findings_given or "")
    assert station.findings_split_status == "complete"
    assert "case summary" not in (station.findings_given or "").lower()


def test_the_case_record_reaches_the_model_as_background_only(db):
    """ELICITED must never draw on it: it is the examiners' own account and
    names the answer throughout."""
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.findings = "Corneal scarring inferiorly."
    station.case_summary = "A 32-year-old with keratoconus."
    db.commit()

    client = _SplitClient({"given": "32 year old.", "elicited": "Corneal scarring inferiorly."})
    split_findings(db, client, station)

    assert "CASE RECORD" in client.user
    assert "never for ELICITED" in client.user


def test_a_diagnosis_carried_in_from_the_case_record_is_still_withheld(db):
    """The guard runs over the whole block, whichever source it came from.

    Drawing on the case summary is new; the case summary names the diagnosis in
    almost every station, so the deterministic check matters more than it did
    when GIVEN could only be built from the findings.
    """
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.findings = "Inferior corneal thinning."
    station.case_summary = "A 32-year-old with keratoconus."
    station.diagnosis = "Keratoconus"
    db.commit()

    client = _SplitClient({
        "given": "32 year old. The patient has keratoconus.",
        "elicited": "Inferior corneal thinning.",
    })
    split_findings(db, client, station)

    assert "keratoconus" not in (station.findings_given or "").lower()
    assert "32 year old" in (station.findings_given or "")
    assert "keratoconus" in (station.findings_elicited or "").lower(), (
        "not discarded - it is a real finding the candidate must reach"
    )


def test_an_acronym_in_the_diagnosis_is_a_giveaway_too(db):
    """Station 26 opened with "He completed TB therapy in 2024".

    The check read words of four letters or more, and "TB" is two - so the
    background named the organism against a diagnosis of "TB-associated
    panuveitis" and nothing noticed.
    """
    from app.services.osce.findings import withhold_diagnosis
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.diagnosis = "TB-associated panuveitis with retinal vasculitis."

    kept, moved = withhold_diagnosis(
        "He completed TB therapy in 2024. Vision: Right 6/9, Left 6/12.", station
    )

    assert "TB therapy" not in kept
    assert "6/9" in kept, "the measurements are what the block is for"
    assert moved


def test_the_same_disease_under_another_name_is_still_the_diagnosis(db):
    """"Uveitic glaucoma" against "panuveitis" shares no whole word.

    Station 26 said both, and word equality let it through: the candidate was
    told the disease in the sentence before being asked to find it.
    """
    from app.services.osce.findings import withhold_diagnosis
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.diagnosis = "TB-associated panuveitis with retinal vasculitis."

    kept, moved = withhold_diagnosis("He has uveitic glaucoma. IOP 24 mmHg.", station)

    assert "uveitic" not in kept
    assert "24 mmHg" in kept
    assert moved


def test_the_anatomy_a_diagnosis_is_named_after_survives(db):
    """The root match must not take the structure down with the disease.

    "Iris" and "iritis" share four letters, and a background that cannot say
    "iris" cannot describe half the anterior segment.
    """
    from app.services.osce.findings import withhold_diagnosis
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.diagnosis = "Acute anterior uveitis"

    kept, _ = withhold_diagnosis("The iris is intact. Vision 6/6.", station)
    assert "iris" in kept.lower()


def test_the_measurements_are_kept_when_only_the_history_leaks(db):
    """A block is not emptied for one bad sentence."""
    from app.services.osce.findings import withhold_diagnosis
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.diagnosis = "Keratoconus"

    kept, moved = withhold_diagnosis(
        "32 year old woman. Known keratoconus since 2019. Vision 6/60 right, 6/7.5 left.",
        station,
    )

    assert "32 year old woman" in kept
    assert "6/60" in kept
    assert len(moved) == 1


def test_a_station_is_never_left_with_no_background_at_all(db):
    """Being too strict makes the station unusable.

    Four stations have a diagnosis written as a history - "Bilateral Myopic
    LASIK", "Unilateral aphakia following corneal laceration repair" - so every
    sentence of their record names it and striking them all left the candidate
    opening on nothing.

    A real station tells them this much: Joshua Bullock's opens "monitored
    2023-2024 with no progression and discharged". Past surgery and injury are
    background, and the marks are for what the candidate finds on examining.
    """
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.findings = "The right eye is aphakic with an irregular pupil."
    station.case_summary = (
        "A 2-year-old girl one week after primary repair of a full thickness "
        "corneal laceration."
    )
    station.diagnosis = "Unilateral aphakia following corneal laceration repair"
    db.commit()

    client = _SplitClient({
        "given": "2 year old girl, one week after repair of a corneal laceration.",
        "elicited": "The right eye is aphakic with an irregular pupil.",
    })
    split_findings(db, client, station)

    assert station.findings_given, "a station with nothing to open on is worse"
    assert "2 year old girl" in station.findings_given


def test_a_block_with_something_safe_still_loses_the_line_that_leaks(db):
    """The rule only relaxes when striking would leave nothing behind."""
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.findings = "Corneal scarring."
    station.diagnosis = "Keratoconus"
    db.commit()

    client = _SplitClient({
        "given": "32 year old woman. She has keratoconus. Vision 6/60 right.",
        "elicited": "Corneal scarring.",
    })
    split_findings(db, client, station)

    assert "keratoconus" not in (station.findings_given or "").lower()
    assert "6/60" in station.findings_given


def test_a_picture_to_open_on_beats_printing_the_diagnosis(db):
    """2019 Semester 2 station 4 opened with the words "Birdshot Chorioretinopathy".

    That report is examiner feedback rather than a case record: the whole
    source block is a one-line summary, the aims and the common mistakes, with
    no history, no acuity and no pressures anywhere in the file. Its clinical
    content is in the slides.

    So "never leave the station with nothing to open on" - which protects a
    station whose history is safe to show - was preserving the answer itself.
    Where there is a figure, the figure is the stem, and the station works the
    way a real one does: look at these images and say what you see.
    """
    from app.models import OsceFigure
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.findings = None
    station.case_summary = "Birdshot Chorioretinopathy"
    station.diagnosis = "Birdshot Chorioretinopathy"
    db.add(OsceFigure(station_id=station.id, position=0,
                      wanted_description="fundus", verification_status="from_paper"))
    db.commit()

    client = _SplitClient({"given": "Birdshot Chorioretinopathy", "elicited": ""})
    split_findings(db, client, station)

    assert not station.findings_given, "the diagnosis must not be the opening screen"
    assert "Birdshot" in (station.findings_elicited or ""), (
        "it is still a real finding - one the candidate reaches, not is handed"
    )


def test_with_no_picture_the_background_is_still_kept(db):
    """The older rule survives where it was right.

    A station with neither a figure nor a background has nothing at all to
    open on, and that was the failure that made the never-empty rule
    necessary. Only the presence of a picture changes the answer.
    """
    from app.services.osce.findings import split_findings
    from tests.test_api_osce import make_station

    station = make_station(db)
    station.figures.clear()
    station.findings = None
    station.case_summary = "Bilateral Myopic LASIK"
    station.diagnosis = "Bilateral Myopic LASIK"
    db.commit()

    client = _SplitClient({"given": "Bilateral Myopic LASIK", "elicited": ""})
    split_findings(db, client, station)

    assert station.findings_given, "with no picture, a bare station is the worse failure"
