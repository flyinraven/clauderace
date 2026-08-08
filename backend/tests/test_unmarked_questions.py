"""A question worth nothing is not a question.

The builder is told the 20 marks must total 20, and that was checked. Nothing
said every question must carry some, so the model concentrated them: 147
questions across 98 stations were worth nothing - one station three of its six
- and the marker replies "This question carries no marks" to an answer that
cost a minute of a nine-minute station. 154 minutes of clock across the bank.
"""

from __future__ import annotations

from app.services.osce.prompts import _unmarked_questions
from app.services.osce.remark import STATION_MARKS, remark_station


def test_a_question_with_no_rubric_is_a_problem():
    problems = _unmarked_questions([
        {"label": "A", "rubric": [{"text": "Describes the opacity", "marks": 20}]},
        {"label": "B", "rubric": []},
    ])
    assert len(problems) == 1
    assert "question B carries no marks" in problems[0]


def test_a_rubric_whose_points_are_all_worth_zero_is_the_same_problem():
    """Ten of the 147 look marked until the marks are added up."""
    problems = _unmarked_questions([
        {"label": "A", "rubric": [{"text": "Something", "marks": 0}]},
    ])
    assert len(problems) == 1


def test_a_fully_marked_station_raises_nothing():
    assert _unmarked_questions([
        {"label": "A", "rubric": [{"text": "x", "marks": 12}]},
        {"label": "B", "rubric": [{"text": "y", "marks": 8}]},
    ]) == []


class _Client:
    """Returns a fixed marking key, so the repair's own arithmetic is tested."""

    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, **kwargs):
        return self.payload


def _Station(prompts):
    """A real model instance: `flag_modified` needs SQLAlchemy's own state."""
    from app.models import OsceStation

    return OsceStation(
        subspecialty="Glaucoma",
        diagnosis="Primary open angle glaucoma",
        findings_elicited="Cupped discs",
        total_marks=20,
        prompts=prompts,
    )


def _db():
    class DB:
        def commit(self):
            pass
    return DB()


def test_the_repair_gives_the_dead_question_marks_and_keeps_the_total():
    station = _Station([
        {"label": "A", "text": "Examine.", "rubric": [{"text": "x", "marks": 20}]},
        {"label": "B", "text": "What next?", "rubric": []},
    ])
    client = _Client({
        "A": [{"text": "x", "marks": 14, "is_critical": True}],
        "B": [{"text": "Names the next step", "marks": 6}],
    })
    outcome = remark_station(_db(), client, station)
    assert outcome == {"remarked": 1, "questions_given_marks": 1}
    marks = [sum(pt["marks"] for pt in p["rubric"]) for p in station.prompts]
    assert marks == [14, 6]
    assert sum(marks) == STATION_MARKS


def test_a_key_that_no_longer_totals_twenty_is_refused():
    """Every candidate would otherwise be marked against a different maximum."""
    station = _Station([
        {"label": "A", "text": "Examine.", "rubric": [{"text": "x", "marks": 20}]},
        {"label": "B", "text": "What next?", "rubric": []},
    ])
    client = _Client({
        "A": [{"text": "x", "marks": 14}],
        "B": [{"text": "y", "marks": 2}],      # totals 16
    })
    outcome = remark_station(_db(), client, station)
    assert outcome["rejected"] == 1
    assert station.prompts[1]["rubric"] == [], "the station is left as it was"


def test_a_key_that_leaves_a_question_dead_is_refused():
    station = _Station([
        {"label": "A", "text": "Examine.", "rubric": [{"text": "x", "marks": 20}]},
        {"label": "B", "text": "What next?", "rubric": []},
    ])
    client = _Client({"A": [{"text": "x", "marks": 20}]})
    outcome = remark_station(_db(), client, station)
    assert outcome["rejected"] == 1
