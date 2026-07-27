"""Automated marking against the model answer key.

Every question in the real RACE written paper is marked by two examiners, so
each answer here is graded by two independent model passes and the awarded
marks averaged. Where the two disagree by more than
`EXAMINER_DISCREPANCY_THRESHOLD` of the available marks the part is flagged for
human review, which is the same thing a real exam board would do.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.constants import DEFAULT_ANGOFF_EXPECTED
from app.models import (
    Answer,
    ExamPaper,
    ExamPaperQuestion,
    ExamSession,
    Grade,
    Question,
    QuestionPart,
    SessionResult,
)
from app.services.ai import AIClient
from app.services.coerce import as_int
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.marking import (
    aggregate_by_key,
    clamp_award,
    examiner_passes,
    temperature_for,
    upsert_grade,
    verdict,
)

logger = logging.getLogger(__name__)

JOB_GRADE_SESSION = "grade_session"

SYSTEM_PROMPT = """\
You are a RANZCO examiner marking one sub-question of a RACE written paper. \
You are given the sub-question, its mark allocation, the official marking key, \
and the candidate's answer.

Mark exactly as an examiner would:
- Award marks key point by key point. A key point is earned if the candidate \
conveys that idea, in any reasonable wording. Accept synonyms, abbreviations \
in common ophthalmic use, and the listed alternatives.
- Partial credit is allowed where a key point is worth more than one mark and \
the candidate covers part of it.
- Do NOT award marks for content that is not in the key, however true. Do NOT \
deduct marks for extra correct material.
- If an answer contains a dangerous or clearly incorrect clinical statement, \
award nothing for the affected key point and say so in the comment.
- Candidates write in note form under time pressure. Do not penalise brevity, \
grammar, or lack of prose.
- Be consistent and fair. The total you award must not exceed the marks \
available for the sub-question.

Return ONLY a JSON object:
{
  "breakdown": [
    {"point_id": <integer as given>,
     "awarded": <number>,
     "comment": "<why this was or was not awarded, one short sentence>"}
  ],
  "awarded_total": <number>,
  "feedback": "<two or three sentences of examiner feedback: what was done
                well, and specifically what was missing>"
}"""


def _build_prompt(part: QuestionPart, question: Question, answer_text: str) -> str:
    key_lines = []
    for point in sorted(part.answer_points, key=lambda p: p.position):
        extras = []
        if point.accepted_alternatives:
            extras.append(f"also accept: {'; '.join(point.accepted_alternatives)}")
        if point.is_critical:
            extras.append("CRITICAL - examiners specifically looked for this")
        suffix = f" ({'; '.join(extras)})" if extras else ""
        key_lines.append(
            f"  point_id={point.id} | {point.marks:g} mark(s) | {point.text}{suffix}"
        )

    return (
        f"SUBSPECIALTY: {question.subspecialty or 'unspecified'}\n"
        f"TOPIC: {question.topic or 'unspecified'}\n\n"
        f"CLINICAL STEM:\n{question.stem or '(none)'}\n\n"
        + (f"SCENARIO CONTINUES: {part.preamble}\n\n" if part.preamble else "")
        + f"SUB-QUESTION ({part.marks:g} marks):\n"
        f"{part.label + ') ' if part.label else ''}{part.text}\n\n"
        f"OFFICIAL MARKING KEY:\n" + "\n".join(key_lines) + "\n\n"
        f"CANDIDATE'S ANSWER:\n\"\"\"\n{answer_text.strip() or '(no answer given)'}\n\"\"\"\n\n"
        f"Mark it now. The maximum you may award is {part.marks:g}."
    )


def grade_part(
    db: Session,
    client: AIClient,
    session: ExamSession,
    part: QuestionPart,
    question: Question,
    answer_text: str,
    examiner_pass: int,
    job_id: int | None = None,
) -> Grade:
    available = float(part.marks)

    # An empty answer scores zero without spending a model call.
    if not answer_text.strip():
        grade = upsert_grade(db, Grade, session.id, examiner_pass, part_id=part.id)
        grade.awarded_marks = 0.0
        grade.available_marks = available
        grade.breakdown = [
            {
                "point_id": p.id,
                "point_text": p.text,
                "marks": p.marks,
                "awarded": 0.0,
                "comment": "No answer given.",
            }
            for p in part.answer_points
        ]
        grade.feedback = "No answer was submitted for this sub-question."
        grade.model_used = "n/a"
        db.commit()
        return grade

    data = client.complete_json(
        task="grading",
        system=SYSTEM_PROMPT,
        user=_build_prompt(part, question, answer_text),
        temperature=temperature_for(examiner_pass),
        job_id=job_id,
    )
    if not isinstance(data, dict):
        raise ValueError("Grading response was not a JSON object")

    points_by_id = {p.id: p for p in part.answer_points}
    breakdown: list[dict[str, Any]] = []
    total = 0.0

    for item in data.get("breakdown") or []:
        if not isinstance(item, dict):
            continue
        point = points_by_id.get(as_int(item.get("point_id")))
        if point is None:
            continue
        awarded = clamp_award(item.get("awarded"), point.marks)
        total += awarded
        breakdown.append(
            {
                "point_id": point.id,
                "point_text": point.text,
                "marks": point.marks,
                "awarded": round(awarded, 2),
                "comment": str(item.get("comment") or "").strip() or None,
                "is_critical": point.is_critical,
            }
        )

    # The per-point sum is authoritative; a stated total that disagrees is the
    # model's arithmetic error, not a marking decision.
    total = min(total, available)

    grade = upsert_grade(db, Grade, session.id, examiner_pass, part_id=part.id)
    grade.awarded_marks = round(total, 2)
    grade.available_marks = available
    grade.breakdown = breakdown
    grade.feedback = str(data.get("feedback") or "").strip() or None
    grade.model_used = client.model_for("grading")
    db.commit()
    return grade


# --- Job handler ----------------------------------------------------------
@register_handler(JOB_GRADE_SESSION)
def handle_grade_session(ctx: JobContext) -> bool:
    """Grade one part per chunk, both examiner passes together."""
    session_id = ctx.payload.get("session_id")
    if not session_id:
        raise JobHandlerError("Grading job is missing session_id")

    session = ctx.db.get(ExamSession, session_id)
    if session is None:
        raise JobHandlerError(f"Sitting {session_id} no longer exists")

    part_ids = ctx.cursor_get("part_ids")
    if part_ids is None:
        part_ids = _parts_for_session(ctx.db, session)
        if ctx.payload.get("only_missing"):
            # Completing a partly-marked paper: skip what already has both
            # examiner passes, so a rate-limited run can be finished cheaply.
            already = _fully_graded_part_ids(ctx.db, session.id)
            part_ids = [p for p in part_ids if p not in already]
        ctx.cursor_set(part_ids=part_ids)
        ctx.set_total(len(part_ids) or 1)
        session.grading_status = "running"
        ctx.db.commit()

    index = ctx.cursor_get("index", 0)
    if index >= len(part_ids):
        summarise_session(ctx.db, session)
        ctx.set_message("Marking complete")
        return True

    part = ctx.db.get(QuestionPart, part_ids[index])
    if part is not None:
        question = ctx.db.get(Question, part.question_id)
        answer = ctx.db.execute(
            select(Answer)
            .where(Answer.session_id == session.id)
            .where(Answer.part_id == part.id)
        ).scalar_one_or_none()
        text = answer.text if answer else ""

        client = AIClient(ctx.db)
        for examiner_pass in examiner_passes(ctx.db):
            try:
                grade_part(
                    ctx.db, client, session, part, question, text, examiner_pass,
                    job_id=ctx.job.id,
                )
            except Exception as exc:  # noqa: BLE001 - one part must not stop the paper
                ctx.db.rollback()
                logger.exception("Grading failed for part %s pass %s", part.id, examiner_pass)
                log_error(
                    ctx.db,
                    source="grading",
                    message=f"Part {part.id} pass {examiner_pass}: {exc}",
                    context={"session_id": session.id, "part_id": part.id},
                )
                failed = list((ctx.job.result or {}).get("failed_parts", []))
                failed.append(part.id)
                ctx.set_result(failed_parts=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Marked {index + 1} of {len(part_ids)} sub-questions")

    if index + 1 >= len(part_ids):
        summarise_session(ctx.db, session)
        return True
    return False


def _fully_graded_part_ids(db: Session, session_id: int) -> set[int]:
    """Parts that already carry both examiner passes."""
    rows = db.execute(
        select(Grade.part_id, func.count(Grade.id))
        .where(Grade.session_id == session_id)
        .group_by(Grade.part_id)
    ).all()
    return {part_id for part_id, count in rows if count >= 2}


def _parts_for_session(db: Session, session: ExamSession) -> list[int]:
    question_ids = db.execute(
        select(ExamPaperQuestion.question_id)
        .where(ExamPaperQuestion.paper_id == session.paper_id)
        .order_by(ExamPaperQuestion.section, ExamPaperQuestion.position)
    ).scalars().all()
    if not question_ids:
        return []

    questions = {
        q.id: q
        for q in db.execute(
            select(Question)
            .where(Question.id.in_(question_ids))
            .options(selectinload(Question.parts).selectinload(QuestionPart.answer_points))
        ).scalars().all()
    }

    part_ids: list[int] = []
    for question_id in question_ids:
        question = questions.get(question_id)
        if question is None:
            continue
        for part in sorted(question.parts, key=lambda p: p.position):
            # A part with no marking key cannot be graded.
            if part.answer_points:
                part_ids.append(part.id)
    return part_ids


# --- Aggregation ----------------------------------------------------------
def summarise_session(db: Session, session: ExamSession) -> SessionResult:
    """Average the two examiner passes and produce the final result.

    If any sub-question could not be marked - a provider rate limit is the
    usual cause - the result is reported as incomplete and NO pass/fail verdict
    is issued. Declaring a fail against a cut score derived from only part of
    the paper would be actively misleading to a candidate.
    """
    grades = db.execute(
        select(Grade).where(Grade.session_id == session.id)
    ).scalars().all()

    expected_part_ids = set(_parts_for_session(db, session))
    graded_part_ids = {g.part_id for g in grades}
    ungraded = sorted(expected_part_ids - graded_part_ids)

    by_part = aggregate_by_key(grades, lambda g: g.part_id)

    # Which subspecialty each part belongs to, in one join rather than two
    # lookups per sub-question.
    subspecialty_by_part = {
        part_id: subspecialty or "Unclassified"
        for part_id, subspecialty in db.execute(
            select(QuestionPart.id, Question.subspecialty)
            .join(Question, Question.id == QuestionPart.question_id)
            .where(QuestionPart.id.in_(list(by_part) or [0]))
        ).all()
    }

    total_awarded = sum(a.awarded for a in by_part.values())
    total_available = sum(a.available for a in by_part.values())
    flagged = sorted(part_id for part_id, a in by_part.items() if a.flagged)

    # The written result also reports where the marks were won and lost, which
    # the OSCE does not: a station is one subspecialty by construction.
    by_subspecialty: dict[str, dict[str, float]] = defaultdict(
        lambda: {"awarded": 0.0, "available": 0.0}
    )
    for part_id, aggregate in by_part.items():
        key = subspecialty_by_part.get(part_id, "Unclassified")
        by_subspecialty[key]["awarded"] += aggregate.awarded
        by_subspecialty[key]["available"] += aggregate.available

    percentage = (total_awarded / total_available * 100) if total_available else 0.0

    paper = db.get(ExamPaper, session.paper_id)
    cut_score = paper.cut_score if paper else None
    if cut_score is None and total_available:
        cut_score = round(total_available * DEFAULT_ANGOFF_EXPECTED, 2)

    # The cut score is set for the whole paper; scale it if only part of the
    # paper could be marked, so the comparison stays like-for-like.
    effective_cut = cut_score
    if cut_score is not None and paper and paper.total_marks and total_available:
        effective_cut = round(cut_score * (total_available / paper.total_marks), 2)

    outcome = verdict(ungraded, total_awarded, effective_cut)

    breakdown = {
        name: {
            "awarded": round(values["awarded"], 2),
            "available": round(values["available"], 2),
            "percentage": round(
                values["awarded"] / values["available"] * 100 if values["available"] else 0.0, 1
            ),
        }
        for name, values in sorted(by_subspecialty.items())
    }

    result = db.execute(
        select(SessionResult).where(SessionResult.session_id == session.id)
    ).scalar_one_or_none()
    if result is None:
        result = SessionResult(session_id=session.id)
        db.add(result)

    result.total_awarded = round(total_awarded, 2)
    result.total_available = round(total_available, 2)
    result.percentage = round(percentage, 1)
    result.cut_score = effective_cut
    result.outcome = outcome
    result.subspecialty_breakdown = breakdown
    result.flagged_parts = flagged
    result.ungraded_parts = ungraded
    result.overall_feedback = _overall_feedback(
        percentage, effective_cut, total_awarded, breakdown, ungraded, len(expected_part_ids)
    )

    session.grading_status = "partial" if ungraded else "complete"
    db.commit()
    return result


def _overall_feedback(
    percentage: float,
    cut_score: float | None,
    awarded: float,
    breakdown: dict[str, dict[str, float]],
    ungraded: list[int],
    expected: int,
) -> str:
    if ungraded:
        # Lead with the caveat: a partial score read as a final one is worse
        # than no score at all.
        return (
            f"This paper could not be fully marked: {len(ungraded)} of {expected} "
            f"sub-questions failed to mark, usually because the AI provider's rate "
            f"limit was reached. The {awarded:.1f} marks shown cover only the "
            f"{expected - len(ungraded)} sub-questions that were marked, so no "
            f"pass/fail verdict can be given. Use 'Re-mark' to complete it."
        )

    lines = [f"You scored {awarded:.1f} marks ({percentage:.1f}%)."]
    if cut_score is not None:
        margin = awarded - cut_score
        verdict = "above" if margin >= 0 else "below"
        lines.append(
            f"The Angoff cut score for this paper is {cut_score:.1f} marks, "
            f"so you are {abs(margin):.1f} marks {verdict} the pass standard."
        )

    ranked = sorted(breakdown.items(), key=lambda kv: kv[1]["percentage"])
    weak = [name for name, values in ranked[:3] if values["percentage"] < 60]
    strong = [name for name, values in reversed(ranked[-3:]) if values["percentage"] >= 75]
    if weak:
        lines.append("Weakest areas: " + ", ".join(weak) + ".")
    if strong:
        lines.append("Strongest areas: " + ", ".join(strong) + ".")
    return " ".join(lines)
