"""A question that states what its own rubric pays for hearing.

`_answers_itself` was written for this and reaches only part of it. It tests
the stem against the station's DIAGNOSIS, through a list of verbs - "shows",
"reveals", "there is". Station 467 of 2016 Semester 1 said "On slit lamp
examination, this patient HAS fine inferior KPs, a low grade anterior chamber
reaction, a PSC cataract, and vitreous cells and debris. What do these
findings suggest to you?" and kept 6.5 marks, every one critical, for
identifying exactly those four - with two slit lamp photographs on screen. The
verb was "has", so nothing fired.

The diagnosis is not the only thing a stem can give away, and no list of verbs
is ever finished. Comparing the stem to the rubric needs neither.

The hard half is not finding these. It is not flagging the 46 questions that
look identical to a word-overlap test and are perfectly well formed: a
question has to name its own subject, so "What are the criteria for ROP
screening?" shares every word with "States the criteria for ROP screening" and
hands over nothing. The difference is asserting versus asking, which is what
`_asserted` exists to separate.
"""

from __future__ import annotations

from app.services.osce.repair import NOT_WORTH_SPENDING_ALONE, REMEDIES
from app.services.osce.sittability import station_faults
from tests.test_api_osce import make_station


def _ask(db, text, rubric, label="A", step=1):
    station = make_station(db)
    station.prompts = [{"label": label, "step": step, "seconds": 90,
                        "text": text, "rubric": rubric}]
    station.total_marks = sum(r["marks"] for r in rubric)
    db.flush()
    return station


def _kinds(station):
    return {f.kind for f in station_faults(station)}


def test_station_467_hands_over_four_critical_marks(db):
    station = _ask(db, (
        "On slit lamp examination, this patient has fine inferior KPs, a low "
        "grade anterior chamber reaction, a PSC cataract, and vitreous cells "
        "and debris. What do these findings suggest to you?"
    ), [
        {"text": "Identify and describe the fine inferior KPs.",
         "marks": 2, "is_critical": True},
        {"text": "Identify and describe the vitreous cells and debris.",
         "marks": 1.5, "is_critical": True},
    ])
    assert "stem_gives_away_rubric" in _kinds(station)


def test_station_10_reveal_may_name_the_diagnosis_but_not_the_complication(db):
    """The reveal question is meant to state the diagnosis - later marks must
    not depend on earlier ones. What it may not do is state the complication
    the rubric pays separately for recognising."""
    station = _ask(db, (
        "The diagnosis is bilateral optic disc drusen with left peripapillary "
        "choroidal neovascularisation. How would you manage her if she were "
        "new to you and you had just made the diagnosis?"
    ), [
        {"text": "Identifies left peripapillary choroidal neovascularisation "
                 "as a complication.", "marks": 2.5, "is_critical": True},
    ], label="D", step=5)
    assert "stem_gives_away_rubric" in _kinds(station)


def test_a_question_naming_its_own_subject_is_not_a_leak(db):
    """Station 208: "What are the criteria for ROP screening, specifically who
    and when should be screened?" against "States the criteria for ROP
    screening (who and when)". Every content word is shared and nothing is
    given away, because the question asks for them rather than saying them."""
    station = _ask(db, (
        "What are the criteria for ROP screening, specifically who and when "
        "should be screened?"
    ), [
        {"text": "States the criteria for ROP screening (who and when).",
         "marks": 2.5, "is_critical": False},
    ], label="E", step=7)
    assert "stem_gives_away_rubric" not in _kinds(station)


def test_an_asserted_clause_before_the_question_still_counts(db):
    """Station 12 asserts a premise and asks something else of it - that is
    fine - but station 435 asserted the findings and then asked for them."""
    fine = _ask(db, (
        "Given the findings on the MRI, how would you distinguish axial from "
        "non-axial proptosis on clinical examination?"
    ), [
        {"text": "Describe clinical methods to distinguish axial vs. non-axial "
                 "proptosis.", "marks": 1.5, "is_critical": False},
    ], label="B", step=2)
    assert "stem_gives_away_rubric" not in _kinds(fine)

    leaking = _ask(db, (
        "You are presented with a 23-year-old physiology student. He has a "
        "2-month history of left acute BRVO with CMO and bilateral vasculitis "
        "and vitritis. Here are some images from his workup. Describe what "
        "they show."
    ), [
        {"text": "Identifies bilateral vasculitis and vitritis.",
         "marks": 1.0, "is_critical": True},
    ])
    assert "stem_gives_away_rubric" in _kinds(leaking)


def test_the_parenthetical_is_the_answer_and_must_be_weighed(db):
    """Station 435 question D: "What are the potential long-term complications
    of TB-associated retinal vasculitis and BRVO?" against "Identifies
    long-term complications of retinal vasculitis (e.g. neovascularisation,
    vitreous haemorrhage...)". Dropping the parenthetical left only the topic,
    which the question must name, and it read as a leak."""
    station = _ask(db, (
        "What are the potential long-term complications of TB-associated "
        "retinal vasculitis and BRVO, and how would you monitor for them?"
    ), [
        {"text": "Identifies long-term complications of retinal vasculitis "
                 "(e.g., neovascularization, vitreous hemorrhage, tractional "
                 "retinal detachment, glaucoma).",
         "marks": 2.0, "is_critical": False},
    ], label="D", step=7)
    assert "stem_gives_away_rubric" not in _kinds(station)


def test_an_unmarked_question_is_a_fault(db):
    """Stations 159 and 116 each asked a question worth nothing, and told the
    candidate so after ninety seconds of a nine-minute station."""
    station = make_station(db)
    station.prompts = [
        {"label": "A", "step": 1, "seconds": 90, "text": "Examine the discs.",
         "rubric": [{"text": "Recognises cupping.", "marks": 20,
                     "is_critical": False}]},
        {"label": "C", "step": 4, "seconds": 90, "rubric": [],
         "text": "What are your differential diagnoses for the progression?"},
    ]
    station.total_marks = 20
    db.flush()
    assert "unmarked_question" in _kinds(station)


def test_neither_kind_can_start_a_run_that_spends():
    """No image fixes a sentence, and no search invents a mark. Both are
    routed to a person, and both are excluded from the set that pulls a
    station into a paid repair - otherwise the reconcile pass would rewrite
    well-formed questions on the strength of a word-overlap heuristic."""
    for kind in ("stem_gives_away_rubric", "unmarked_question"):
        assert REMEDIES[kind] == "human"
        assert kind in NOT_WORTH_SPENDING_ALONE


def test_a_station_marked_on_examining_the_patient_needs_a_view_of_them(db):
    """Station 90 paid 7.5 of 20 marks for examining the cranial nerves and
    naming the 5th, 7th, 8th and 12th palsies, and showed one close-up of a
    cornea. The candidate scored 2.5. Thirty-seven never-sat stations are like
    this - an orbit station saying "please examine the orbits" over a coronal
    CT - five of them with all 20 marks resting on it."""
    from app.models import Image, OsceFigure

    station = make_station(db)
    image = Image(sha256="c" * 64, content_type="image/jpeg", data=b"j",
                  size_bytes=1, origin="pdf")
    db.add(image)
    db.flush()
    db.add(OsceFigure(station_id=station.id, position=0, image_id=image.id,
                      is_approved=True, caption="Coronal CT scan of the orbits"))
    station.prompts = [{"label": "A", "step": 1, "seconds": 180,
                        "text": "Please examine the orbits and eyelids.",
                        "rubric": [{"text": "Identifies the proptosis and ptosis.",
                                    "marks": 20, "is_critical": True}]}]
    station.total_marks = 20
    db.flush()
    db.refresh(station)
    assert "no_view_of_the_patient" in _kinds(station)


def test_a_photograph_of_the_patient_settles_it(db):
    """The same station with an external photograph is fine, and a station
    with no image at all is `no_opening_image`'s business, not this one."""
    from app.models import Image, OsceFigure

    station = make_station(db)
    image = Image(sha256="d" * 64, content_type="image/jpeg", data=b"j",
                  size_bytes=1, origin="pdf")
    db.add(image)
    db.flush()
    db.add(OsceFigure(station_id=station.id, position=0, image_id=image.id,
                      is_approved=True, caption="External photograph of both eyes"))
    station.prompts = [{"label": "A", "step": 1, "seconds": 180,
                        "text": "Please examine the orbits and eyelids.",
                        "rubric": [{"text": "Identifies the proptosis and ptosis.",
                                    "marks": 20, "is_critical": True}]}]
    station.total_marks = 20
    db.flush()
    db.refresh(station)
    assert "no_view_of_the_patient" not in _kinds(station)


def test_this_fault_can_never_start_a_search():
    """Searching is what produced the wrong images. Nothing about this fault
    makes the next search better than the last, so it goes to a person."""
    assert REMEDIES["no_view_of_the_patient"] == "human"
    assert "no_view_of_the_patient" in NOT_WORTH_SPENDING_ALONE


def test_a_preamble_mentioning_both_eyes_is_not_asking_for_both(db):
    """Station 616: "This young lady has reduced vision of 6/36 in both eyes.
    Please examine the right fundus." It asks for one eye and says which. The
    checker matched the acuity preamble, and the repair that followed rewrote
    the acuity into "reduced vision of 6/36 the right eye"."""
    from app.models import Image, OsceFigure

    station = make_station(db)
    image = Image(sha256="e" * 64, content_type="image/jpeg", data=b"j",
                  size_bytes=1, origin="pdf")
    db.add(image)
    db.flush()
    db.add(OsceFigure(station_id=station.id, position=0, image_id=image.id,
                      is_approved=True, caption="Fundus photograph of the right eye"))
    station.prompts = [{"label": "A", "step": 1, "seconds": 180,
                        "text": "This young lady has reduced vision of 6/36 in "
                                "both eyes. Please examine the right fundus.",
                        "rubric": [{"text": "Describes the macular atrophy.",
                                    "marks": 20, "is_critical": True}]}]
    station.total_marks = 20
    db.flush()
    db.refresh(station)
    assert "missing_side" not in _kinds(station)
