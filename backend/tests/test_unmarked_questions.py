"""A question worth nothing is not a question.

The builder is told the 20 marks must total 20, and that was checked. Nothing
said every question must carry some, so the model concentrated them: 147
questions across 98 stations were worth nothing - one station three of its six
- and the marker replies "This question carries no marks" to an answer that
cost a minute of a nine-minute station. 154 minutes of clock across the bank.
"""

from __future__ import annotations

from app.services.osce.prompts import _unmarked_questions
from app.services.osce.remark import STATION_MARKS, plan_marks, remark_station


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
    """Returns marking-point wording only, which is all the model is asked for."""

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


def test_the_allocation_totals_twenty_and_leaves_no_question_dead():
    """The arithmetic is done here, not by the model. Asking a model to hit an
    exact sum across six questions failed on 84 of 98 stations."""
    targets = plan_marks([
        {"label": "A", "seconds": 180, "rubric": [{"marks": 10}]},
        {"label": "B", "seconds": 90, "rubric": []},
        {"label": "C", "seconds": 90, "rubric": []},
        {"label": "D", "seconds": 90, "rubric": []},
        {"label": "E", "seconds": 60, "rubric": [{"marks": 6}]},
        {"label": "F", "seconds": 30, "rubric": [{"marks": 4}]},
    ])
    assert sum(targets.values()) == STATION_MARKS
    assert all(v >= 1 for v in targets.values())
    assert targets["B"] == targets["C"] == targets["D"], "equal time, equal worth"


def test_a_revived_question_cannot_become_the_biggest_on_the_station():
    targets = plan_marks([
        {"label": "A", "seconds": 60, "rubric": [{"marks": 20}]},
        {"label": "B", "seconds": 480, "rubric": []},
    ])
    assert targets["B"] <= 4.0
    assert sum(targets.values()) == STATION_MARKS


def test_an_allocation_that_cannot_work_is_refused():
    """Nothing to take the marks from."""
    assert plan_marks([{"label": "A", "seconds": 60, "rubric": []}]) is None


def test_the_repair_writes_the_points_and_the_marks_it_planned():
    station = _Station([
        {"label": "A", "text": "Examine.", "seconds": 300, "rubric": [{"text": "x", "marks": 20}]},
        {"label": "B", "text": "What next?", "seconds": 240, "rubric": []},
    ])
    client = _Client({"B": ["Names the next step", "Gives a reason"]})
    outcome = remark_station(_db(), client, station)

    assert outcome == {"remarked": 1, "questions_given_marks": 1}
    marks = [sum(pt["marks"] for pt in p["rubric"]) for p in station.prompts]
    assert sum(marks) == STATION_MARKS
    assert all(m >= 1 for m in marks)
    assert [pt["text"] for pt in station.prompts[1]["rubric"]] == [
        "Names the next step", "Gives a reason",
    ]
    # The surviving point is scaled, not deleted: it is still a thing the
    # candidate is credited for saying.
    assert station.prompts[0]["rubric"][0]["text"] == "x"


def test_a_question_the_model_wrote_no_points_for_is_refused():
    station = _Station([
        {"label": "A", "text": "Examine.", "seconds": 300, "rubric": [{"text": "x", "marks": 20}]},
        {"label": "B", "text": "What next?", "seconds": 240, "rubric": []},
    ])
    outcome = remark_station(_db(), _Client({}), station)
    assert outcome["rejected"] == 1
    assert "B" in outcome["reason"]
    assert station.prompts[1]["rubric"] == [], "the station is left as it was"


def test_a_question_worth_one_mark_does_not_come_back_worth_one_and_a_half():
    """The 0.5 that refused eleven stations for totalling 20.5.

    Every point had a floor of half a mark and the remainder was pushed onto
    one of them, which cannot go below zero. Four points on a question worth
    one mark therefore came to 1.5, and the half surfaced on the station total
    - reported as an allocation that could not be made, when the allocation
    was right and the sharing out was not.
    """
    from app.services.osce.remark import _spread

    points = [{"text": f"point {i}"} for i in range(4)]
    _spread(points, 1.0)

    assert sum(pt["marks"] for pt in points) == 1.0
    assert all(pt["marks"] > 0 for pt in points), "a point worth nothing is the same fault"


def test_more_points_than_half_marks_loses_the_ones_that_cannot_be_paid():
    """Half a mark is the finest award, so one mark buys two points at most."""
    from app.services.osce.remark import _spread

    points = [{"text": f"point {i}"} for i in range(6)]
    _spread(points, 1.5)

    assert len(points) == 3
    assert sum(pt["marks"] for pt in points) == 1.5


def test_an_examiners_own_points_are_never_cut_to_pay_for_a_revival():
    """A question the examiners wrote four lines for cannot be worth one mark.

    Trimming is for points this pass wrote itself. The station's own rubric is
    not its to discard, so the allocation gives every question at least half a
    mark per line it already holds.
    """
    targets = plan_marks([
        {"label": "A", "seconds": 60,
         "rubric": [{"marks": 1}, {"marks": 1}, {"marks": 1}, {"marks": 1}]},
        {"label": "B", "seconds": 60, "rubric": [{"marks": 16}]},
        {"label": "C", "seconds": 480, "rubric": []},
    ])
    assert targets["A"] >= 2.0, "four lines cost at least two marks"
    assert sum(targets.values()) == STATION_MARKS


def test_the_repair_leaves_no_rubric_line_worth_nothing():
    """End to end: the station totals 20 and every line on it is awardable."""
    station = _Station([
        {"label": "A", "text": "Examine.", "seconds": 480,
         "rubric": [{"text": "x", "marks": 19}]},
        {"label": "B", "text": "What next?", "seconds": 30, "rubric": []},
    ])
    client = _Client({"B": ["One", "Two", "Three", "Four"]})
    outcome = remark_station(_db(), client, station)

    assert outcome["remarked"] == 1
    lines = [pt for p in station.prompts for pt in p["rubric"]]
    assert all(pt["marks"] > 0 for pt in lines), "every line has to be awardable"
    assert sum(pt["marks"] for pt in lines) == STATION_MARKS
