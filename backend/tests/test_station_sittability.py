"""Every image failure found in the live bank, as a test that must keep passing.

These are not hypotheticals. Each one was a station a candidate actually met,
found by reading stations on the site rather than by any check in the codebase -
which is the point. The pipeline's stages each reported success while the
assembled station was unanswerable, because no stage owned the assembled
station. `station_faults` is where that now lives, so this is where the
regressions are pinned.
"""

from __future__ import annotations

from app.models import Image, OsceFigure, OsceStation
from app.services.osce.sittability import is_sittable, opening_figures, station_faults
from tests.test_api_osce import make_station


def _image(db, tag: str, origin: str = "web") -> Image:
    image = Image(sha256=tag * 64, content_type="image/jpeg", data=b"jpeg",
                  size_bytes=4, origin=origin)
    db.add(image)
    db.flush()
    return image


def _figure(db, station, image=None, **kw) -> OsceFigure:
    figure = OsceFigure(
        station_id=station.id,
        position=kw.pop("position", len(station.figures)),
        image_id=image.id if image else None,
        is_approved=kw.pop("is_approved", True),
        verification_status=kw.pop("verification_status", "faithful"),
        match_confidence=kw.pop("match_confidence", 0.9),
        **kw,
    )
    db.add(figure)
    db.flush()
    return figure


def _kinds(station) -> set[str]:
    return {f.kind for f in station_faults(station)}


MOTILITY_TASK = {
    "label": "A", "text": "Please examine the patient's eye movements.",
    "seconds": 270, "rubric": [{"text": "Identifies the gaze palsy", "marks": 10}],
}
SCAN_QUESTION = {
    "label": "C", "text": "What does this scan show?", "seconds": 90,
    "image_wanted": "MRI of the brain showing white matter lesions",
    "rubric": [{"text": "Reads the scan", "marks": 5}],
}


def test_a_questions_scan_is_not_the_stations_opening_image(db):
    """Station 158: examine the eye movements, over two brain MRIs.

    The MRI belongs to question C, correctly. Counting it as the station's own
    image made 158 look covered, so no gaze montage was ever searched for.
    """
    station = make_station(db, prompts=[MOTILITY_TASK, SCAN_QUESTION])
    mri = _figure(db, station, _image(db, "a", origin="pdf"))
    station.prompts = [MOTILITY_TASK, {**SCAN_QUESTION, "figure_id": mri.id}]
    db.commit()
    db.refresh(station)

    assert opening_figures(station) == [], "the MRI is question C's"
    assert "no_opening_image" in _kinds(station)
    assert not is_sittable(station)


def test_a_question_presenting_an_image_it_never_asked_for_is_a_fault(db):
    """Station 164: "These are the corneal topography and biometry for both eyes."

    No image_wanted was ever recorded, so nothing was sourced and nothing could
    be. The candidate read a blank screen.
    """
    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment of both eyes.",
         "seconds": 270, "rubric": [{"text": "Describes the findings", "marks": 15}]},
        {"label": "C", "seconds": 90,
         "text": "These are the corneal topography and biometry for both eyes. "
                 "What do they show?",
         "rubric": [{"text": "Reads them", "marks": 5}]},
    ])
    _figure(db, station, _image(db, "b"))
    db.commit()
    db.refresh(station)

    faults = station_faults(station)
    assert "presents_nothing" in {f.kind for f in faults}
    # A person must reword it or supply the image; searching again cannot help.
    assert not next(f for f in faults if f.kind == "presents_nothing").fixable_by_sourcing


def test_the_same_photograph_twice_is_a_fault(db):
    """Station 156 showed one gaze montage three times.

    Four of its six rubric points were technique marks - "demonstrates a good
    approach", "performs cover test correctly" - and each became a view
    demanding an image, so the same photograph was attached once per point.
    """
    station = make_station(db, prompts=[MOTILITY_TASK])
    montage = _image(db, "c")
    _figure(db, station, montage, position=0)
    _figure(db, station, montage, position=1)
    db.commit()
    db.refresh(station)

    assert "duplicate_image" in _kinds(station)


def test_an_unapproved_image_shows_the_candidate_nothing(db):
    """Held for review is not the same as present, and used to count as present."""
    station = make_station(db, prompts=[MOTILITY_TASK])
    _figure(db, station, _image(db, "d"), is_approved=False,
            verification_status="representative")
    db.commit()
    db.refresh(station)

    kinds = _kinds(station)
    assert "not_approved" in kinds and "representative_only" in kinds


def test_an_impossible_request_is_not_reported_as_merely_missing(db):
    """A serology titre and a textbook diagram are not waiting on a better query.

    Counting them with the searchable ones made the backlog look bigger than
    the work was, and they were bought again on every run.
    """
    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment.", "seconds": 400,
         "rubric": [{"text": "Describes it", "marks": 18}]},
        {"label": "C", "text": "What does this show?", "seconds": 70,
         "image_wanted": "QuantiFERON-TB Gold test result, showing a positive result.",
         "rubric": [{"text": "Reads it", "marks": 2}]},
    ])
    _figure(db, station, _image(db, "e"))
    db.commit()
    db.refresh(station)

    faults = [f for f in station_faults(station) if f.kind == "impossible_request"]
    assert faults and not faults[0].fixable_by_sourcing


def test_a_station_that_can_be_answered_reports_nothing(db):
    """The check has to be able to say yes, or it is just noise."""
    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment of the left eye.",
         "seconds": 400, "rubric": [{"text": "Describes the corneal opacity", "marks": 18}]},
        {"label": "B", "text": "How would you manage her?", "seconds": 140,
         "rubric": [{"text": "Gives a plan", "marks": 2}]},
    ])
    _figure(db, station, _image(db, "f"), verification_status="faithful",
            match_confidence=0.92, is_approved=True)
    db.commit()
    db.refresh(station)

    assert station_faults(station) == []
    assert is_sittable(station)


def test_a_rejected_figure_is_a_decision_taken_not_one_outstanding(db):
    """Station 164 carried three refused images and reported three faults.

    None of them was actionable: the images had been judged and set aside. The
    audit counted them as work waiting to be done, which is how a backlog comes
    to look larger than it is.
    """
    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment of both eyes.",
         "seconds": 400, "rubric": [{"text": "Describes the findings", "marks": 18}]},
        {"label": "B", "text": "How would you manage her?", "seconds": 140,
         "rubric": [{"text": "Gives a plan", "marks": 2}]},
    ])
    _figure(db, station, _image(db, "g"), position=0, verification_status="faithful",
            match_confidence=0.9, is_approved=True)
    for tag, position in (("h", 1), ("i", 2)):
        _figure(db, station, _image(db, tag), position=position,
                verification_status="rejected", is_approved=False)
    db.commit()
    db.refresh(station)

    assert station_faults(station) == [], "only the approved opening image counts"
