"""A candidate reading their result has to be told what they should have said.

A rubric point is written for an examiner holding a pen - "Identifies the
inferior corneal thinning" - and the grading comment only says the mark was
missed. Between them they never state the answer. The written papers have
shown their model answer on review since they existed; the OSCE never did.
"""

from __future__ import annotations

from app.services.osce.model_answers import (
    stations_needing_model_answers,
    write_model_answers,
)
from app.api.osce.sittings import _with_model_answers


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        self.user = kwargs.get("user", "")
        return self.payload


def _db():
    class DB:
        def commit(self):
            pass

        def rollback(self):
            pass

    return DB()


def _station(prompts):
    from app.models import OsceStation

    return OsceStation(
        subspecialty="Cornea",
        diagnosis="Keratoconus",
        findings_elicited="Inferior corneal thinning with Vogt striae.",
        total_marks=20,
        prompts=prompts,
    )


TWO_QUESTIONS = [
    {"label": "A", "text": "Examine the cornea.", "seconds": 300,
     "rubric": [{"text": "Identifies the inferior thinning", "marks": 5},
                {"text": "Notes the Vogt striae", "marks": 5}]},
    {"label": "B", "text": "How would you manage her?", "seconds": 240,
     "rubric": [{"text": "Offers cross-linking", "marks": 10}]},
]


def test_every_marking_point_is_numbered_across_the_whole_station():
    """One call per station, so the numbering cannot restart at each question.

    Numbering per question put three sets of "0, 1" in one reply and landed
    each answer on whichever question was read last.
    """
    station = _station([dict(p, rubric=[dict(r) for r in p["rubric"]]) for p in TWO_QUESTIONS])
    client = _Client({"0": "The inferior cornea is thinned.",
                      "1": "There are vertical striae in the deep stroma.",
                      "2": "I would offer corneal cross-linking."})

    outcome = write_model_answers(_db(), client, station)

    assert outcome["written"] == 3
    assert client.calls == 1, "one station, one call"
    assert station.prompts[0]["rubric"][0]["model_answer"] == "The inferior cornea is thinned."
    assert station.prompts[1]["rubric"][0]["model_answer"] == "I would offer corneal cross-linking."


def test_points_that_already_have_an_answer_are_not_paid_for_again():
    prompts = [dict(TWO_QUESTIONS[0], rubric=[
        {"text": "Identifies the inferior thinning", "marks": 5,
         "model_answer": "Already written."},
        {"text": "Notes the Vogt striae", "marks": 5},
    ])]
    station = _station(prompts)
    client = _Client({"0": "There are vertical striae in the deep stroma."})

    outcome = write_model_answers(_db(), client, station)

    assert outcome["written"] == 1
    assert "Identifies the inferior thinning" not in client.user, (
        "a point with an answer is not sent to the model again"
    )
    assert station.prompts[0]["rubric"][0]["model_answer"] == "Already written."


def test_a_station_whose_answers_are_all_written_is_left_alone():
    station = _station([{"label": "A", "text": "x", "seconds": 60, "rubric": [
        {"text": "y", "marks": 20, "model_answer": "z"},
    ]}])
    client = _Client({})
    assert write_model_answers(_db(), client, station) == {"already_written": 1}
    assert client.calls == 0


def test_a_reply_with_nothing_in_it_changes_nothing():
    station = _station([dict(p, rubric=[dict(r) for r in p["rubric"]]) for p in TWO_QUESTIONS])
    outcome = write_model_answers(_db(), _Client({}), station)
    assert outcome["rejected"] == 1
    assert all(
        "model_answer" not in pt
        for p in station.prompts for pt in p["rubric"]
    ), "the station is left as it was"


def test_the_review_joins_the_answer_onto_the_point_it_belongs_to():
    """Written once for the question, read by every review of it - including
    the sittings already over, which is most of them."""
    prompt = {"rubric": [
        {"text": "Identifies the inferior thinning", "marks": 5,
         "model_answer": "The inferior cornea is thinned."},
        {"text": "Notes the Vogt striae", "marks": 5},
    ]}
    breakdown = [
        {"index": 0, "text": "Identifies the inferior thinning", "marks": 5,
         "awarded": 2.5, "comment": "Said thinning but not where."},
        {"index": 1, "text": "Notes the Vogt striae", "marks": 5,
         "awarded": 0, "comment": "Not mentioned."},
    ]

    out = _with_model_answers(breakdown, prompt)

    assert out[0]["model_answer"] == "The inferior cornea is thinned."
    assert out[1]["model_answer"] is None
    assert out[0]["awarded"] == 2.5, "the record of what happened is untouched"


def test_an_answer_is_never_shown_under_a_point_it_was_not_written_for():
    """A rubric rewritten since the sitting must not relabel the marking.

    The breakdown holds the key the candidate was actually marked against. If
    the station's rubric has moved on, the index still resolves - to a
    different point - and the answer would appear under someone else's text.
    """
    prompt = {"rubric": [
        {"text": "A completely different point now", "marks": 5,
         "model_answer": "The answer to the new point."},
    ]}
    breakdown = [
        {"index": 0, "text": "Identifies the inferior thinning", "marks": 5,
         "awarded": 5, "comment": "Said it."},
    ]

    out = _with_model_answers(breakdown, prompt)
    assert out[0]["model_answer"] is None
    assert out[0]["text"] == "Identifies the inferior thinning"


def test_a_sitting_with_no_breakdown_survives_the_join():
    assert _with_model_answers(None, {"rubric": []}) is None
    assert _with_model_answers([], {"rubric": []}) == []


def test_the_bank_pass_skips_stations_that_are_done(db):
    from tests.test_api_osce import make_station

    done = make_station(db, prompts=[{"label": "A", "text": "x", "seconds": 60,
                                      "rubric": [{"text": "y", "marks": 20,
                                                  "model_answer": "z"}]}])
    todo = make_station(db, prompts=[{"label": "A", "text": "x", "seconds": 60,
                                      "rubric": [{"text": "y", "marks": 20}]}])
    db.commit()

    ids = stations_needing_model_answers(db)
    assert todo.id in ids
    assert done.id not in ids
