"""A grade with nothing in it is a failure, not a score of nothing.

`grade_prompt` built its breakdown by walking the model's reply and skipping
anything it could not read. Where it could read none of it - an empty
`breakdown`, or every index out of range - `total` stayed 0.0 and that was
committed as the candidate's mark, with no comment to show why.

Station 577 question D took a 699-character answer on managing intermittent
exotropia and recorded 0 of 4.5, with an empty breakdown and no feedback. It
was not in `ungraded_prompts`, so nothing flagged it: the sitting reported
2/20 and read as a candidate who knew nothing. Across the bank that was 59
marks over 13 questions.

This is the principle `unmarkable_reason` already sets out for transcription,
one stage later - a wrong mark is worse than a missing one, because a missing
one says so.
"""

from __future__ import annotations

import pytest

from app.models import OsceGrade, OsceSession
from app.services.osce.circuit import grade_prompt
from tests.test_api_osce import make_station


class _Client:
    """An AI client whose reply parses as JSON and carries no usable marks."""

    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, **kwargs):
        return self.payload

    def model_for(self, task):
        return "test-model"


def _sitting(db, station):
    session = OsceSession(user_id=1, station_id=station.id, is_timed=False)
    db.add(session)
    db.flush()
    return session


PROMPT = {
    "label": "D", "step": 5, "seconds": 120,
    "text": "The diagnosis is intermittent exotropia. How would you manage him?",
    "rubric": [
        {"text": "Discusses prism correction.", "marks": 2.5},
        {"text": "Discusses surgical options.", "marks": 2.0},
    ],
}


@pytest.mark.parametrize("payload", [
    {"breakdown": [], "feedback": ""},
    {"breakdown": None},
    {"feedback": "good answer"},
    # Every index outside the rubric, so nothing survives the walk.
    {"breakdown": [{"index": 9, "awarded": 2.0}, {"index": -1, "awarded": 1.0}]},
])
def test_an_unreadable_reply_raises_instead_of_scoring_zero(db, payload):
    station = make_station(db)
    session = _sitting(db, station)
    with pytest.raises(ValueError, match="no usable breakdown"):
        grade_prompt(db, _Client(payload), session, station, PROMPT,
                     "I would prescribe prisms and consider surgery.", 1)


def test_a_question_worth_nothing_still_scores_nothing_quietly(db):
    """The other empty breakdown is legitimate and must not start raising:
    a question carrying no rubric has nothing to mark against."""
    station = make_station(db)
    session = _sitting(db, station)
    grade = grade_prompt(db, _Client({}), session, station,
                         {"label": "C", "step": 4, "seconds": 90,
                          "text": "Anything?", "rubric": []},
                         "some answer", 1)
    assert grade.awarded_marks == 0.0
    assert grade.feedback == "This question carries no marks."


def test_a_readable_reply_is_still_marked(db):
    station = make_station(db)
    session = _sitting(db, station)
    grade = grade_prompt(
        db, _Client({"breakdown": [{"index": 0, "awarded": 2.5,
                                    "comment": "said prisms"}],
                     "feedback": "well done"}),
        session, station, PROMPT, "I would prescribe prisms.", 1)
    assert grade.awarded_marks == 2.5
    assert len(grade.breakdown) == 1


def test_the_marker_is_told_what_was_on_the_screen(db):
    """Nothing in the marking chain knew this. The rubric comes from the
    examiners' report, model answers are written from the diagnosis, and the
    marker saw neither the pictures nor their captions - so station 309 scored
    a candidate 0/5.5 for describing the photograph in front of them."""
    from app.models import Image, OsceFigure
    from app.services.osce.circuit import _prompt_for

    station = make_station(db)
    image = Image(sha256="f" * 64, content_type="image/jpeg", data=b"j",
                  size_bytes=1, origin="pdf")
    db.add(image)
    db.flush()
    shown = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                       is_approved=True,
                       caption="External photograph of the left eye")
    db.add(shown)
    db.flush()
    db.refresh(station)

    prompt = {"label": "A", "step": 1, "seconds": 180,
              "text": "Describe your findings.",
              "rubric": [{"text": "Recognises corneal hydrops.", "marks": 5}]}
    user = _prompt_for(station, prompt, "There is a dense corneal opacity.")
    assert "ON THE CANDIDATE'S SCREEN" in user
    assert "External photograph of the left eye" in user


def test_a_question_answered_from_memory_says_so(db):
    from app.services.osce.circuit import _prompt_for

    station = make_station(db)
    db.flush()
    db.refresh(station)
    user = _prompt_for(station, {"label": "E", "step": 7, "seconds": 60,
                                 "text": "Name three associations.",
                                 "rubric": [{"text": "Names three.", "marks": 3}]},
                       "Sarcoid, TB, syphilis.")
    assert "answered from memory" in user
