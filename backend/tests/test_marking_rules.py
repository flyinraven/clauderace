"""The marking rules shared by written papers and OSCE stations.

These were duplicated in `grading.grade` and `osce.circuit`. Now that both flows
depend on one copy, a change here changes every result the platform issues, so
each rule is pinned directly rather than only through the two end-to-end flows.
"""

from __future__ import annotations

import pytest

from app.constants import EXAMINER_DISCREPANCY_THRESHOLD
from app.models import Grade, OsceGrade, Setting
from app.services.marking import (
    absorb_mark_drift,
    aggregate_by_key,
    aggregate_passes,
    clamp_award,
    examiner_passes,
    rescale_marks_to_awardable,
    temperature_for,
    upsert_grade,
    verdict,
)


class FakeGrade:
    """Enough of a Grade to aggregate: both real models expose these two."""

    def __init__(self, awarded: float, available: float, key: str = "A"):
        self.awarded_marks = awarded
        self.available_marks = available
        self.key = key


# --- Clamping ------------------------------------------------------------
@pytest.mark.parametrize(
    ("awarded", "maximum", "expected"),
    [
        (3, 6, 3.0),
        (99, 6, 6.0),          # a model that misread the rubric
        (-5, 6, 0.0),          # not a thing an examiner can do
        (6, 6, 6.0),
        (2.5, 6, 2.5),         # half marks are legitimate
        ("4", 6, 4.0),         # models return numbers as strings
        (None, 6, 0.0),
        ("not a number", 6, 0.0),
        (3, 0, 0.0),           # a point worth nothing awards nothing
    ],
)
def test_an_award_is_clamped_to_what_the_point_is_worth(awarded, maximum, expected):
    assert clamp_award(awarded, maximum) == expected


# --- Averaging the passes ------------------------------------------------
def test_one_pass_is_taken_as_it_stands():
    result = aggregate_passes([FakeGrade(4, 10)])
    assert result.awarded == 4.0
    assert result.available == 10.0
    assert result.flagged is False, "one examiner cannot disagree with themselves"


def test_two_passes_are_averaged():
    result = aggregate_passes([FakeGrade(4, 10), FakeGrade(6, 10)])
    assert result.awarded == 5.0
    assert result.available == 10.0


def test_examiners_disagreeing_past_the_threshold_flags_the_part():
    """What a real exam board would arbitrate."""
    spread = (EXAMINER_DISCREPANCY_THRESHOLD * 10) + 1
    assert aggregate_passes([FakeGrade(0, 10), FakeGrade(spread, 10)]).flagged is True


def test_examiners_close_enough_together_are_not_flagged():
    below = (EXAMINER_DISCREPANCY_THRESHOLD * 10) - 0.5
    assert aggregate_passes([FakeGrade(0, 10), FakeGrade(below, 10)]).flagged is False


def test_a_part_worth_nothing_cannot_be_flagged():
    """Dividing the spread by zero available marks is not a disagreement."""
    assert aggregate_passes([FakeGrade(0, 0), FakeGrade(0, 0)]).flagged is False


def test_the_larger_available_figure_wins_if_the_passes_disagree_on_it():
    """They should always agree; if they do not, do not silently mark out of less."""
    assert aggregate_passes([FakeGrade(2, 8), FakeGrade(2, 10)]).available == 10.0


def test_grouping_averages_each_unit_separately():
    grades = [
        FakeGrade(4, 10, "A"), FakeGrade(6, 10, "A"),
        FakeGrade(1, 10, "B"),
    ]
    by_key = aggregate_by_key(grades, lambda g: g.key)
    assert by_key["A"].awarded == 5.0
    assert by_key["B"].awarded == 1.0
    assert sum(a.available for a in by_key.values()) == 20.0


# --- The verdict ---------------------------------------------------------
def test_a_partly_marked_result_gets_no_verdict_however_high_the_score():
    """The rule that stops a candidate reading a partial score as a final one."""
    assert verdict([7], total_awarded=95.0, cut_score=50.0) == "incomplete"


def test_pass_and_fail_are_decided_against_the_cut_score():
    assert verdict([], total_awarded=50.0, cut_score=50.0) == "pass", "on the line passes"
    assert verdict([], total_awarded=49.9, cut_score=50.0) == "fail"


def test_no_cut_score_means_no_verdict_rather_than_a_fail():
    assert verdict([], total_awarded=10.0, cut_score=None) is None


# --- Examiner passes -----------------------------------------------------
def test_one_pass_by_default(db):
    assert examiner_passes(db) == (1,)


def test_two_passes_when_configured(db):
    db.add(Setting(key="grading.examiner_passes", value=2, is_encrypted=False))
    db.commit()
    assert examiner_passes(db) == (1, 2)


@pytest.mark.parametrize("configured", [0, -1, 5, 99])
def test_the_pass_count_is_clamped_to_one_or_two(db, configured):
    """Zero passes would mark nothing; five would quintuple the bill."""
    db.add(Setting(key="grading.examiner_passes", value=configured, is_encrypted=False))
    db.commit()
    assert examiner_passes(db) in {(1,), (1, 2)}


def test_the_two_passes_use_different_temperatures():
    """Two samples at the same temperature are not two opinions."""
    assert temperature_for(1) != temperature_for(2)
    assert temperature_for(1) == 0.0, "the first pass is deterministic"
    assert temperature_for(99) == 0.2, "an unexpected pass number still marks"


# --- Grade rows ----------------------------------------------------------
def test_a_grade_row_is_created_once_and_then_reused(db):
    """Re-marking must not accumulate a second row per attempt."""
    first = upsert_grade(db, Grade, session_id=1, examiner_pass=1, part_id=7)
    first.awarded_marks = 3.0
    db.commit()

    again = upsert_grade(db, Grade, session_id=1, examiner_pass=1, part_id=7)
    assert again.id == first.id
    assert db.query(Grade).count() == 1


def test_each_pass_and_each_unit_gets_its_own_row(db):
    upsert_grade(db, Grade, session_id=1, examiner_pass=1, part_id=7)
    upsert_grade(db, Grade, session_id=1, examiner_pass=2, part_id=7)
    upsert_grade(db, Grade, session_id=1, examiner_pass=1, part_id=8)
    upsert_grade(db, Grade, session_id=2, examiner_pass=1, part_id=7)
    db.commit()
    assert db.query(Grade).count() == 4


def test_the_same_helper_serves_the_osce_key(db):
    """A station identifies its markable unit by prompt label, not part id."""
    grade = upsert_grade(db, OsceGrade, session_id=1, examiner_pass=1, prompt_label="A")
    grade.available_marks = 10.0
    db.commit()

    assert upsert_grade(
        db, OsceGrade, session_id=1, examiner_pass=1, prompt_label="A"
    ).id == grade.id
    upsert_grade(db, OsceGrade, session_id=1, examiner_pass=1, prompt_label="B")
    db.commit()
    assert db.query(OsceGrade).count() == 2


# --- Mark arithmetic -----------------------------------------------------
def test_rounding_drift_lands_on_the_largest_point():
    """Eight marks over three points is 2.67 each, which sums to 8.01."""
    points = [{"marks": 1.0}, {"marks": 2.67}, {"marks": 4.34}]
    assert sum(p["marks"] for p in points) == 8.01, "the drift this exists to fix"

    absorb_mark_drift(points, 8.0)
    assert sum(p["marks"] for p in points) == 8.0
    # The largest point absorbed it; the others are untouched.
    assert points[2]["marks"] == 4.33
    assert points[0]["marks"] == 1.0
    assert points[1]["marks"] == 2.67


def test_drift_is_absorbed_downwards_too():
    points = [{"marks": 7.0}, {"marks": 7.0}, {"marks": 7.0}]
    absorb_mark_drift(points, 20.0)
    assert sum(p["marks"] for p in points) == 20.0


def test_a_point_is_never_pushed_negative():
    points = [{"marks": 0.5}, {"marks": 30.0}]
    absorb_mark_drift(points, 1.0)
    assert all(p["marks"] >= 0 for p in points)


def test_a_total_already_correct_is_left_alone():
    points = [{"marks": 10.0}, {"marks": 10.0}]
    absorb_mark_drift(points, 20.0)
    assert [p["marks"] for p in points] == [10.0, 10.0]


def test_no_points_is_not_an_error():
    absorb_mark_drift([], 20.0)  # a station whose rubric came back empty


# --- Awardable-mark rescaling ---------------------------------------------
# Proportional rescaling followed by 2dp rounding produced rubric lines worth
# 1.54 and 3.06 marks, and station totals like 9.99. No examiner can award
# those, so the OSCE builder apportions half marks instead - the granularity
# clamp_award has always accepted.


def _marks(points):
    return [p["marks"] for p in points]


def test_rescaled_marks_are_awardable_and_total_exactly():
    # The rubric that shipped as 1.54 / 2.31 / 3.06 / ... on a real station.
    points = [
        {"marks": m}
        for m in (1.54, 2.31, 3.06, 1.54, 1.54, 1.54, 2.31, 3.08, 1.54, 1.54)
    ]
    assert rescale_marks_to_awardable(points, 20) is True
    marks = _marks(points)
    assert all((m * 2) == int(m * 2) for m in marks), marks
    assert sum(marks) == 20


def test_whole_marks_stay_ints_so_a_key_reads_2_not_2_point_0():
    points = [{"marks": 1.0}, {"marks": 1.0}]
    assert rescale_marks_to_awardable(points, 20) is True
    assert _marks(points) == [10, 10]
    assert all(isinstance(m, int) for m in _marks(points))


def test_proportions_survive_the_rescale():
    points = [{"marks": 1.0}, {"marks": 1.0}, {"marks": 2.0}]
    assert rescale_marks_to_awardable(points, 20) is True
    assert _marks(points) == [5, 5, 10]


def test_a_sub_question_of_small_lines_is_not_inflated():
    """The bug that made this half marks rather than whole ones.

    Eight lines worth half a mark each are worth four marks. Forcing a whole
    mark minimum made them worth eight, taken off whoever was heaviest.
    """
    small = [{"marks": 0.5} for _ in range(8)]
    large = [{"marks": 16.0}]
    points = small + large
    assert rescale_marks_to_awardable(points, 20) is True
    assert sum(_marks(points[:8])) == 4, "the small lines keep their weight"
    assert points[8]["marks"] == 16
    assert sum(_marks(points)) == 20


def test_no_rubric_line_is_left_worth_nothing():
    points = [{"marks": 100.0}, {"marks": 0.1}, {"marks": 0.1}]
    assert rescale_marks_to_awardable(points, 20) is True
    assert all(p["marks"] >= 0.5 for p in points)
    assert sum(_marks(points)) == 20


def test_more_lines_than_half_marks_declines_rather_than_dropping_any():
    points = [{"marks": 1.0} for _ in range(41)]
    assert rescale_marks_to_awardable(points, 20) is False
    # Untouched, so the caller can fall back to fractions with all 41 intact.
    assert _marks(points) == [1.0] * 41


def test_the_stations_whole_marks_could_not_serve_are_now_fine():
    """28 rubric lines over 20 marks: impossible whole, easy in halves."""
    points = [{"marks": 0.71} for _ in range(28)]
    assert rescale_marks_to_awardable(points, 20) is True
    assert sum(_marks(points)) == 20
    assert all(p["marks"] >= 0.5 for p in points)


def test_exactly_as_many_lines_as_half_marks_gives_half_each():
    points = [{"marks": 3.0} for _ in range(40)]
    assert rescale_marks_to_awardable(points, 20) is True
    assert _marks(points) == [0.5] * 40


def test_nothing_to_scale_is_declined_not_crashed():
    assert rescale_marks_to_awardable([], 20) is False
    assert rescale_marks_to_awardable([{"marks": 0.0}], 20) is False
    assert rescale_marks_to_awardable([{"marks": 1.0}], 0) is False
