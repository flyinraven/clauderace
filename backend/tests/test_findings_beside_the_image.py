"""A picture with the findings printed under it hands over the answer.

`described_findings` exists as the last resort for a figure whose image could
not be found: the examiner says what the investigation showed, and the
candidate answers from words. `visible_figure` returned it whenever it was
approved, without ever checking whether an image had turned up too - so where
both existed the candidate got the photograph and, directly beneath it, the
findings they were being marked on finding.

Station 623 showed a nine-position gaze montage captioned "External
photographs of both eyes in nine positions of gaze" and below it: "There is a
left hypertropia of 10 prism diopters in primary gaze. There is a positive
3-step test. There is mild left inferior oblique overaction. There is an
abnormal head posture." Question A awards one critical mark for each of those
four, and the panel spoke all four. 721 figures across the bank showed an
image with findings beside it; 461 of them repeated a rubric item.

The examiner still states what a real examiner states. Acuity, pressure and
refraction are handed over at every station and are recorded as
`findings_given`, so sentences grounded there survive; the signs the candidate
is there to elicit do not.
"""

from __future__ import annotations

from app.api.osce.helpers import visible_figure
from app.models import Image, OsceFigure
from tests.test_api_osce import make_station


def _figure(db, station, described, with_image=True):
    image = None
    if with_image:
        image = Image(sha256="a" * 64, content_type="image/jpeg", data=b"jpeg",
                      size_bytes=4, origin="pdf")
        db.add(image)
        db.flush()
    figure = OsceFigure(
        station_id=station.id,
        position=0,
        image_id=image.id if image else None,
        is_approved=True,
        caption="Figure",
        described_findings=described,
        described_findings_approved=True,
    )
    db.add(figure)
    db.flush()
    db.refresh(figure)
    return figure


def test_signs_are_not_printed_under_the_picture(db):
    """Station 623: four critical marks spoken beneath the montage."""
    station = make_station(db)
    station.findings_given = (
        "Patient presents with symptomatic diplopia of one year's duration. "
        "Prescribed prismatic spectacles."
    )
    db.flush()
    figure = _figure(db, station, (
        "There is a left hypertropia of 10 prism diopters in primary gaze. "
        "There is a positive 3-step test. There is mild left inferior oblique "
        "overaction. There is an abnormal head posture."
    ))
    payload = visible_figure(figure)
    assert payload is not None, "the image must still be shown"
    assert payload["image_id"] is not None
    assert payload["described_findings"] is None


def test_what_the_examiner_really_states_survives(db):
    """Acuity and pressure are given at every station and must not be lost."""
    station = make_station(db)
    station.findings_given = (
        "42 year old female Visual disturbance in her left eye "
        "Visual acuity Right 6/4.8 Visual acuity Left 6/9.5 "
        "Intraocular pressure Right 14 mmHg Intraocular pressure Left 17 mmHg"
    )
    db.flush()
    figure = _figure(db, station, (
        "Visual acuity is 6/4.8 in the right eye and 6/9.5 in the left eye. "
        "Intraocular pressures are 14 mmHg in the right eye and 17 mmHg in "
        "the left eye."
    ))
    kept = visible_figure(figure)["described_findings"]
    assert kept is not None
    assert "6/4.8" in kept
    # "pressures" against a given "pressure" is the same handover.
    assert "17 mmHg" in kept


def test_one_extra_word_can_still_be_the_whole_mark(db):
    """Station 116: "There is lash ptosis." is three critical marks, and it
    differs from a given history of "progressive ptosis" by one word."""
    station = make_station(db)
    station.findings_given = (
        "46 year old male 7-year history of progressive ptosis "
        "Visual acuity RE 6/12 Visual acuity LE 6/12"
    )
    db.flush()
    figure = _figure(db, station, "There is lash ptosis.")
    assert visible_figure(figure)["described_findings"] is None


def test_words_alone_are_untouched_when_no_image_arrived(db):
    """The last resort still works. A figure with no picture keeps every word,
    or a search that found nothing would leave the question unanswerable."""
    station = make_station(db)
    station.findings_given = "60 year old man"
    db.flush()
    described = ("The optic disc is swollen with blurred margins and "
                 "peripapillary haemorrhages.")
    figure = _figure(db, station, described, with_image=False)
    assert visible_figure(figure)["described_findings"] == described


def test_a_figure_never_disappears_because_its_words_were_removed(db):
    """Trimming must not empty a figure that has a picture to show."""
    station = make_station(db)
    station.findings_given = "70 year old woman"
    db.flush()
    figure = _figure(db, station, "There is a dense cataract in the left eye.")
    payload = visible_figure(figure)
    assert payload is not None
    assert payload["image_id"] is not None


def test_station_90_words_beside_the_photograph_are_still_the_answer(db):
    """The first fix handled a picture and words on ONE figure. Station 90 put
    them on two: a slit lamp view of the cornea, and beside it a separate
    words-only figure reading "There is left ptosis. The left eye has a corneal
    opacity and lipid keratopathy. The left side of the tongue is atrophic and
    deviates to the left on protrusion." Question A pays 5 of its 9.5 marks for
    saying exactly that. 226 figures were doing this."""
    station = make_station(db)
    station.findings_given = "He has a history of tarsorrhaphy and lid load."
    db.flush()
    # The real station 90: the photograph carries this description too, and
    # the words-only figure repeats it. That repetition is the test - a
    # station whose picture cannot show what the words say keeps them.
    picture = _figure(db, station, (
        "There is a corneal opacity and lipid keratopathy in the left eye. "
        "The left side of the tongue is deviated and atrophic. There is left "
        "ptosis."
    ))
    picture.caption = "External photograph of the left eye"
    words = _figure(db, station, (
        "There is left ptosis. The left eye has a corneal opacity and lipid "
        "keratopathy. The left side of the tongue is atrophic."
    ), with_image=False)
    station.prompts = [{"label": "A", "step": 1, "seconds": 90,
                        "text": "Please examine the cranial nerves.",
                        "rubric": [{"text": "Identifies corneal opacity and lipid "
                                            "keratopathy.", "marks": 20}]}]
    db.flush()
    assert visible_figure(picture) is not None
    assert visible_figure(words) is None


def test_words_a_question_asked_for_are_never_silenced(db):
    """A words-only figure BOUND to a question is the result of an
    investigation the candidate asked for. Removing it leaves the question
    unanswerable, which is the worse failure - so boundness is the test, not
    whether the station happens to hold pictures."""
    station = make_station(db)
    station.findings_given = "62 year old woman"
    db.flush()
    _figure(db, station, None).described_findings = None
    words = _figure(db, station, (
        "The visual field shows a dense superior arcuate defect respecting "
        "the horizontal midline."
    ), with_image=False)
    station.prompts = [{"label": "C", "step": 3, "seconds": 90,
                        "figure_ids": [words.id],
                        "text": "These are her visual fields. What do they show?",
                        "rubric": [{"text": "Reads the arcuate defect.", "marks": 20}]}]
    db.flush()
    assert visible_figure(words)["described_findings"].startswith("The visual field")
