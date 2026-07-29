"""Rules shared by written-paper marking and OSCE station marking.

The two flows mark different things - one a typed answer against a key point,
the other a spoken answer against a rubric line - but they are the same exam
board doing it, and the rules that make a result defensible are identical:

  - two passes at different temperatures, averaged
  - a pass may never award more than the point it is marking is worth
  - examiners disagreeing by more than a threshold flags the part for review
  - a partly-marked paper gets NO pass/fail verdict
  - marks that do not add up are indefensible, so rounding drift is absorbed

Those rules were written twice, once in `grading.grade` and once in
`osce.circuit`, and `circuit` reached into `grade` for a private helper to share
the sixth. They live here now. What stays in each module is what genuinely
differs: the prompt, the shape of a breakdown row, the cut score (a written
paper's is set per paper and scaled when only part of it could be marked; a
station's comes from its own Angoff expectation), and the wording of the
candidate-facing feedback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import EXAMINER_DISCREPANCY_THRESHOLD
from app.services.coerce import as_float

# Two independent passes, deliberately at different temperatures so they are not
# identical samples of the same distribution.
EXAMINER_TEMPERATURES = {1: 0.0, 2: 0.35}
FALLBACK_TEMPERATURE = 0.2

# Rounding to 2dp point by point lets a total drift off the marks available
# (eight marks over three points gives 2.66 x 3 = 7.98). Below this the drift is
# not worth chasing.
MARK_DRIFT_TOLERANCE = 0.005


def temperature_for(examiner_pass: int) -> float:
    return EXAMINER_TEMPERATURES.get(examiner_pass, FALLBACK_TEMPERATURE)


def examiner_passes(db: Session) -> tuple[int, ...]:
    """Which examiner passes to run.

    Two passes reproduce the real exam's double marking and reveal where
    examiners would disagree, at exactly double the cost. One is the sensible
    default for solo revision.
    """
    from app.services.settings_store import SettingsStore

    count = max(1, min(2, SettingsStore(db).get_int("grading.examiner_passes", 1)))
    return tuple(range(1, count + 1))


def clamp_award(awarded: Any, maximum: float) -> float:
    """Never let a pass award more than the point being marked is worth.

    A model that returns 99 for a 6-mark point has misread the rubric, not found
    99 marks of merit, and a negative award is not a thing an examiner can do.
    """
    return max(0.0, min(as_float(awarded, 0.0), float(maximum)))


def upsert_grade(db: Session, model: type, session_id: int, examiner_pass: int, **key: Any):
    """Fetch or create the grade row for one pass over one markable unit.

    `key` is what identifies that unit in this model - `part_id` for a written
    sub-question, `prompt_label` for a station question. Re-marking must reuse
    the row rather than accumulate a second one per attempt.
    """
    stmt = (
        select(model)
        .where(model.session_id == session_id)
        .where(model.examiner_pass == examiner_pass)
    )
    for column, value in key.items():
        stmt = stmt.where(getattr(model, column) == value)

    existing = db.execute(stmt).scalar_one_or_none()
    if existing is not None:
        return existing

    grade = model(session_id=session_id, examiner_pass=examiner_pass, **key)
    db.add(grade)
    db.flush()
    return grade


@dataclass(frozen=True)
class Aggregate:
    """One markable unit's marks, averaged across the examiner passes."""

    awarded: float
    available: float
    # The passes disagreed by more than the board would tolerate. A real exam
    # board arbitrates these; here they are surfaced on the result.
    flagged: bool


def aggregate_passes(grades: list[Any]) -> Aggregate:
    available = max(g.available_marks for g in grades)
    awarded = sum(g.awarded_marks for g in grades) / len(grades)

    flagged = False
    if len(grades) > 1 and available > 0:
        spread = max(g.awarded_marks for g in grades) - min(g.awarded_marks for g in grades)
        flagged = spread / available > EXAMINER_DISCREPANCY_THRESHOLD

    return Aggregate(awarded=awarded, available=available, flagged=flagged)


def aggregate_by_key(grades: list[Any], key_of) -> dict[Any, Aggregate]:
    """Group grades by the unit they mark, then average each unit's passes."""
    grouped: dict[Any, list[Any]] = {}
    for grade in grades:
        grouped.setdefault(key_of(grade), []).append(grade)
    return {key: aggregate_passes(rows) for key, rows in grouped.items()}


def verdict(ungraded: list[Any], total_awarded: float, cut_score: float | None) -> str | None:
    """pass, fail, incomplete, or no verdict at all.

    A verdict is only meaningful when everything was marked. Declaring a fail
    against a cut score derived from part of a paper would actively mislead a
    candidate, so an incomplete result says so and offers a re-mark instead.
    """
    if ungraded:
        return "incomplete"
    if cut_score is None:
        return None
    return "pass" if total_awarded >= cut_score else "fail"


def absorb_mark_drift(points: list[dict[str, Any]], available: float) -> None:
    """Put the rounding remainder on the largest point, in place.

    Marks that do not add up are indefensible to a candidate, and the largest
    point is where a fractional correction is least visible.
    """
    if not points:
        return
    drift = round(available - sum(as_float(p.get("marks"), 0.0) for p in points), 2)
    if abs(drift) < MARK_DRIFT_TOLERANCE:
        return
    target = max(points, key=lambda p: as_float(p.get("marks"), 0.0))
    target["marks"] = round(max(0.0, as_float(target.get("marks"), 0.0) + drift), 2)


def rescale_marks_to_awardable(points: list[dict[str, Any]], available: float) -> bool:
    """Rescale `points` in place to half marks totalling `available`.

    A rubric line worth 3.06 marks is not something an examiner could ever
    award, so proportional rescaling followed by 2dp rounding produces a key
    no candidate can be marked against. Largest-remainder apportionment fixes
    that, but the granularity it apportions in matters more than it looks.

    Half marks, not whole ones. `clamp_award` has always accepted 2.5 as a
    legitimate award, and forcing whole marks moves far more than it repairs:
    the minimum award per line has to be a whole mark, so a sub-question made
    of eight half-mark lines inflates to eight marks and a genuinely heavy one
    is stripped to pay for it. Measured against the live bank that shifted 23%
    of sub-questions by more than a mark, one of them from 3.97 to 8 - a
    redistribution of what candidates are marked out of, dressed up as
    rounding. In half marks the same bank moves by a quarter mark on average
    and nothing by more than one.

    Returns False and leaves the points untouched when even half marks cannot
    be shared out - more lines than half marks available, or nothing to scale.
    """
    # Work in half-mark units so the arithmetic stays in integers.
    units = int(round(available * 2))
    if not points or units <= 0 or len(points) > units:
        return False

    raw = [max(0.0, as_float(p.get("marks"), 0.0)) for p in points]
    total = sum(raw)
    if total <= 0:
        return False

    # Floor each share, then hand the leftover units to the largest remainders.
    exact = [value * units / total for value in raw]
    awarded = [max(1, int(value)) for value in exact]
    leftover = units - sum(awarded)

    # Rank by how far each line still falls short of its exact share, not by
    # its raw fractional part. A line the `max(1, ...)` floor already lifted
    # has been paid above its share, and ordering on the raw fraction handed
    # it a second unit as well - which is how eight lines worth 0.44 became a
    # sub-question worth 7 marks instead of 4.
    while leftover > 0:
        order = sorted(range(len(points)), key=lambda i: exact[i] - awarded[i], reverse=True)
        for index in order:
            if leftover == 0:
                break
            awarded[index] += 1
            leftover -= 1
    # The `max(1, ...)` floor can overshoot when many lines round down to zero;
    # claw the excess back off the largest, never below half a mark each.
    while leftover < 0:
        index = max(range(len(points)), key=lambda i: awarded[i])
        if awarded[index] <= 1:
            return False
        awarded[index] -= 1
        leftover += 1

    for point, marks in zip(points, awarded):
        # Whole marks stay ints so a key reads "2 marks", not "2.0 marks".
        point["marks"] = marks // 2 if marks % 2 == 0 else marks / 2
    return True


__all__ = [
    "Aggregate",
    "EXAMINER_TEMPERATURES",
    "absorb_mark_drift",
    "rescale_marks_to_awardable",
    "aggregate_by_key",
    "aggregate_passes",
    "clamp_award",
    "examiner_passes",
    "temperature_for",
    "upsert_grade",
    "verdict",
]
