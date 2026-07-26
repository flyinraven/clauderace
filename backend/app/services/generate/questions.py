"""Curriculum-aligned question generation.

The RANZCO examiners' reports publish SEQs but no VSAQs, so Part B of every
paper has to be generated from scratch. Questions are produced together with
their marking key in a single call: a VSAQ's key is only two marks, and
generating both at once keeps the question and its answer consistent.

Real ingested questions are used as few-shot style anchors so generated items
read like RANZCO items rather than generic exam questions.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import (
    QUESTION_SEQ,
    QUESTION_VSAQ,
    SEQ_TOTAL_MARKS,
    SOURCE_GENERATED,
    STATUS_REVIEW,
    SUBSPECIALTIES,
    VSAQ_TOTAL_MARKS,
    normalise_subspecialty,
)
from app.models import ModelAnswerPoint, Question, QuestionPart
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_GENERATE_QUESTIONS = "generate_questions"

# Generated per model call. Small enough that one bad response costs little,
# large enough to amortise the style examples in the prompt.
VSAQ_BATCH_SIZE = 5
SEQ_BATCH_SIZE = 1

VSAQ_SYSTEM = f"""\
You are a RANZCO examiner writing Part B of a RACE (RANZCO Advanced Clinical \
Examination) written paper. Part B consists of Very Short Answer Questions.

A VSAQ is NOT a miniature essay question. Its properties are fixed:
- Worth exactly {VSAQ_TOTAL_MARKS} marks.
- A candidate has about 90 seconds to answer it.
- It asks for a small, specific, enumerable answer - typically two facts, two \
causes, two steps, a diagnosis plus one feature, or a single number with its \
justification.
- The answer is a few words to two short lines. If a competent candidate would \
need a paragraph, the question is too big.

Good VSAQ shapes:
  "List two (2) ocular side effects of hydroxychloroquine."
  "A 70-year-old has giant cell arteritis. State the two (2) blood tests you \
would order urgently and the expected finding in each."
  "Name the two (2) muscles that intort the eye."

Bad (these are SEQs, not VSAQs):
  "Outline your management of..."  "Discuss the role of..."

Standard: a trainee ready for independent consultant practice in Australia or \
New Zealand. Reflect ANZ practice, terminology (Snellen 6/6 notation), and \
name landmark trials where an examiner would expect them.

For each question also give the marking key: discrete key points whose marks \
total EXACTLY {VSAQ_TOTAL_MARKS}. Usually two 1-mark points.

Return ONLY a JSON object:
{{
  "questions": [
    {{
      "topic": "short descriptive title",
      "subspecialty": "one of the nine listed",
      "curriculum_codes": ["e.g. Glaucoma CL 5.2"],
      "purpose": "what this question tests, one sentence",
      "question": "the full question text as printed on the paper",
      "difficulty": "easy" | "moderate" | "hard",
      "angoff_expected": <0-1, fraction of marks a borderline candidate scores>,
      "key_points": [
        {{"text": "markable point", "marks": <number>,
          "accepted_alternatives": [string],
          "rationale": "why this earns the mark" | null}}
      ]
    }}
  ]
}}"""

SEQ_SYSTEM = f"""\
You are a RANZCO examiner writing a Short Essay Question for Part A of a RACE \
written paper.

An SEQ is worth exactly {SEQ_TOTAL_MARKS} marks and takes about 15 minutes. It \
opens with a realistic clinical vignette (age, presenting complaint, relevant \
history, examination findings with real numbers - visual acuities in Snellen \
6/x notation, IOPs, refraction) and then asks 4 to 8 lettered sub-questions \
whose marks total exactly {SEQ_TOTAL_MARKS}.

Sub-questions should progress: recognition and interpretation, then \
differential, then investigation, then management, then counselling or \
complications. Where the scenario should advance partway through, put that new \
information in the following part's "preamble".

Standard: a trainee ready for independent consultant practice in Australia or \
New Zealand. Name landmark trials where an examiner would expect them.

Also produce the marking key for every sub-question: discrete key points whose \
marks total EXACTLY that sub-question's allocation.

Return ONLY a JSON object:
{{
  "questions": [
    {{
      "topic": "short descriptive title",
      "subspecialty": "one of the nine listed",
      "curriculum_codes": [string],
      "purpose": "what this question tests, one sentence",
      "stem": "the clinical vignette",
      "difficulty": "easy" | "moderate" | "hard",
      "angoff_expected": <0-1>,
      "image_needed": "description of the clinical image this question should
                       show, or null if none is needed",
      "parts": [
        {{"label": "a", "preamble": string | null, "text": "the sub-question",
          "marks": <number>,
          "key_points": [
            {{"text": "markable point", "marks": <number>,
              "accepted_alternatives": [string], "rationale": string | null}}
          ]}}
      ]
    }}
  ]
}}"""


def _style_examples(db: Session, subspecialty: str | None, limit: int = 2) -> str:
    """Real ingested questions, used as style anchors."""
    stmt = (
        select(Question)
        .where(Question.source == "past_paper")
        .where(Question.question_type == QUESTION_SEQ)
        .order_by(func.random())
        .limit(limit)
    )
    if subspecialty:
        stmt = stmt.where(Question.subspecialty == subspecialty)
    examples = db.execute(stmt).scalars().all()

    if not examples:
        return ""
    blocks = []
    for question in examples:
        parts = "\n".join(
            f"  {p.label}) {p.text} ({p.marks:g} marks)"
            for p in sorted(question.parts, key=lambda p: p.position)
        )
        blocks.append(f"TOPIC: {question.topic}\nSTEM: {question.stem}\n{parts}")
    return (
        "\n\nReal RANZCO questions, for tone and level only - do NOT reuse their "
        "content:\n\n" + "\n\n---\n\n".join(blocks)
    )


def _existing_topics(db: Session, subspecialty: str | None) -> list[str]:
    stmt = select(Question.topic).where(Question.topic.is_not(None))
    if subspecialty:
        stmt = stmt.where(Question.subspecialty == subspecialty)
    return [t for t in db.execute(stmt).scalars().all() if t]


def generate_batch(
    db: Session,
    client: AIClient,
    question_type: str,
    subspecialty: str | None,
    count: int,
    difficulty: str | None = None,
    job_id: int | None = None,
) -> list[int]:
    """Generate a batch of questions with their marking keys. Returns new ids."""
    is_vsaq = question_type == QUESTION_VSAQ
    system = VSAQ_SYSTEM if is_vsaq else SEQ_SYSTEM

    avoid = _existing_topics(db, subspecialty)
    random.shuffle(avoid)

    lines = [
        f"Write {count} new {question_type}(s).",
        f"Subspecialty: {subspecialty or 'any of the nine, spread them out'}.",
        f"The nine subspecialties are: {', '.join(SUBSPECIALTIES)}.",
    ]
    if difficulty:
        lines.append(f"Target difficulty: {difficulty}.")
    if avoid:
        lines.append(
            "These topics are already in the bank - choose materially different "
            "ones:\n  " + "\n  ".join(f"- {t}" for t in avoid[:40])
        )
    lines.append(_style_examples(db, subspecialty))

    data = client.complete_json(
        task="generation", system=system, user="\n".join(lines), job_id=job_id
    )
    if isinstance(data, list):
        data = {"questions": data}
    if not isinstance(data, dict):
        raise ValueError("Generation response was not a JSON object")

    created: list[int] = []
    for spec in data.get("questions") or []:
        if not isinstance(spec, dict):
            continue
        try:
            question_id = (
                _persist_vsaq(db, spec) if is_vsaq else _persist_seq(db, spec)
            )
            if question_id:
                created.append(question_id)
        except Exception:  # noqa: BLE001 - one bad item must not lose the batch
            db.rollback()
            logger.exception("Could not persist a generated question")
    return created


# --- Persistence ----------------------------------------------------------
def _persist_vsaq(db: Session, spec: dict[str, Any]) -> int | None:
    text = _clean(spec.get("question"))
    if not text:
        return None
    if _is_duplicate(db, spec.get("topic"), text):
        logger.info("Skipping near-duplicate VSAQ: %s", spec.get("topic"))
        return None

    question = Question(
        question_type=QUESTION_VSAQ,
        subspecialty=normalise_subspecialty(spec.get("subspecialty")),
        topic=_clean(spec.get("topic")),
        purpose=_clean(spec.get("purpose")),
        # A VSAQ has no separate vignette; the question text carries everything.
        stem="",
        curriculum_codes=_string_list(spec.get("curriculum_codes")) or None,
        total_marks=VSAQ_TOTAL_MARKS,
        source=SOURCE_GENERATED,
        status=STATUS_REVIEW,
        difficulty=_clean(spec.get("difficulty")),
        angoff_expected=_angoff(spec.get("angoff_expected")),
        model_answer_status="complete",
        generation_meta={"generated": True},
    )
    db.add(question)
    db.flush()

    part = QuestionPart(
        question_id=question.id, label=None, position=0, text=text, marks=VSAQ_TOTAL_MARKS
    )
    db.add(part)
    db.flush()

    _persist_key_points(db, part, spec.get("key_points") or [], VSAQ_TOTAL_MARKS)
    db.commit()
    return question.id


def _persist_seq(db: Session, spec: dict[str, Any]) -> int | None:
    stem = _clean(spec.get("stem"))
    parts_spec = [p for p in (spec.get("parts") or []) if isinstance(p, dict) and p.get("text")]
    if not stem or not parts_spec:
        return None
    if _is_duplicate(db, spec.get("topic"), stem):
        logger.info("Skipping near-duplicate SEQ: %s", spec.get("topic"))
        return None

    total = sum(_marks(p.get("marks")) for p in parts_spec)
    question = Question(
        question_type=QUESTION_SEQ,
        subspecialty=normalise_subspecialty(spec.get("subspecialty")),
        topic=_clean(spec.get("topic")),
        purpose=_clean(spec.get("purpose")),
        stem=stem,
        curriculum_codes=_string_list(spec.get("curriculum_codes")) or None,
        total_marks=int(total) if float(total).is_integer() else total,
        source=SOURCE_GENERATED,
        status=STATUS_REVIEW,
        difficulty=_clean(spec.get("difficulty")),
        angoff_expected=_angoff(spec.get("angoff_expected")),
        model_answer_status="complete",
        generation_meta={
            "generated": True,
            "image_needed": _clean(spec.get("image_needed")),
            "warnings": (
                [f"Sub-question marks total {total:g}, expected {SEQ_TOTAL_MARKS}."]
                if abs(total - SEQ_TOTAL_MARKS) > 0.01 else []
            ),
        },
    )
    db.add(question)
    db.flush()

    for index, part_spec in enumerate(parts_spec):
        part = QuestionPart(
            question_id=question.id,
            label=_clean(part_spec.get("label")) or chr(ord("a") + index),
            position=index,
            text=_clean(part_spec.get("text")) or "",
            marks=_marks(part_spec.get("marks")),
            preamble=_clean(part_spec.get("preamble")),
        )
        db.add(part)
        db.flush()
        _persist_key_points(db, part, part_spec.get("key_points") or [], part.marks)

    db.commit()
    return question.id


def _persist_key_points(
    db: Session, part: QuestionPart, points: list[Any], available: float
) -> None:
    clean = [p for p in points if isinstance(p, dict) and p.get("text")]
    if not clean:
        return

    total = sum(_marks(p.get("marks")) for p in clean)
    if total > 0 and abs(total - available) > 0.01:
        # Rescale rather than discard: the content is usually right even when
        # the arithmetic drifts.
        factor = available / total
        for point in clean:
            point["marks"] = round(_marks(point.get("marks")) * factor, 2)
        _absorb_rounding(clean, available)
    elif total == 0:
        even = available / len(clean)
        for point in clean:
            point["marks"] = round(even, 2)
        _absorb_rounding(clean, available)

    for position, point in enumerate(clean):
        db.add(
            ModelAnswerPoint(
                part_id=part.id,
                position=position,
                text=str(point["text"]).strip(),
                marks=_marks(point.get("marks")),
                is_critical=bool(point.get("is_critical")),
                from_examiner_feedback=False,
                rationale=_clean(point.get("rationale")),
                accepted_alternatives=_string_list(point.get("accepted_alternatives")) or None,
            )
        )


def _absorb_rounding(points: list[dict[str, Any]], available: float) -> None:
    """Push the rounding remainder onto the largest key point.

    Rounding each point to 2dp independently lets the sum drift off the marks
    available (eight marks over three points gives 2.66 x 3 = 7.98). Marks that
    do not add up are indefensible to a candidate, so the largest point absorbs
    the difference.
    """
    if not points:
        return
    drift = round(available - sum(_marks(p.get("marks")) for p in points), 2)
    if abs(drift) < 0.005:
        return
    target = max(points, key=lambda p: _marks(p.get("marks")))
    target["marks"] = round(max(0.0, _marks(target.get("marks")) + drift), 2)


def _is_duplicate(db: Session, topic: str | None, text: str) -> bool:
    """Reject near-duplicates by topic match or heavy word overlap.

    Deliberately cheap - no embeddings. Generated batches repeat topics far more
    often than they repeat phrasing, so topic matching catches most of it.
    """
    if topic:
        existing = db.execute(
            select(Question.id).where(func.lower(Question.topic) == topic.strip().lower())
        ).first()
        if existing:
            return True

    signature = _signature(text)
    if not signature:
        return False
    for other in db.execute(
        select(QuestionPart.text).join(Question).where(Question.source == SOURCE_GENERATED)
    ).scalars().all():
        other_signature = _signature(other)
        if not other_signature:
            continue
        overlap = len(signature & other_signature) / max(1, len(signature | other_signature))
        if overlap > 0.75:
            return True
    return False


_STOPWORDS = {
    "the", "a", "an", "of", "in", "for", "and", "or", "to", "with", "on", "at",
    "is", "are", "what", "which", "your", "you", "list", "name", "state", "give",
    "two", "three", "four", "five", "patient", "would",
}


def _signature(text: str) -> set[str]:
    words = re.findall(r"[a-z]{4,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


# --- Job handler ----------------------------------------------------------
@register_handler(JOB_GENERATE_QUESTIONS)
def handle_generate_questions(ctx: JobContext) -> bool:
    """Generate questions one batch per chunk."""
    question_type = (ctx.payload.get("question_type") or QUESTION_VSAQ).upper()
    target = int(ctx.payload.get("count") or 0)
    subspecialties: list[str | None] = ctx.payload.get("subspecialties") or [None]
    difficulty = ctx.payload.get("difficulty")

    if target <= 0:
        raise JobHandlerError("count must be greater than zero")

    batch_size = VSAQ_BATCH_SIZE if question_type == QUESTION_VSAQ else SEQ_BATCH_SIZE

    # Plan the batches once so progress is meaningful and resumable.
    plan: list[dict[str, Any]] = ctx.cursor_get("plan")
    if plan is None:
        plan = []
        remaining = target
        index = 0
        while remaining > 0:
            size = min(batch_size, remaining)
            plan.append(
                {"subspecialty": subspecialties[index % len(subspecialties)], "count": size}
            )
            remaining -= size
            index += 1
        ctx.cursor_set(plan=plan)
        ctx.set_total(len(plan))

    position = ctx.cursor_get("position", 0)
    if position >= len(plan):
        return True

    step = plan[position]
    client = AIClient(ctx.db)
    try:
        created = generate_batch(
            ctx.db, client, question_type, step["subspecialty"], step["count"],
            difficulty=difficulty, job_id=ctx.job.id,
        )
        done = list((ctx.job.result or {}).get("created", []))
        done.extend(created)
        ctx.set_result(created=done, requested=target)
    except Exception as exc:  # noqa: BLE001 - one batch must not stop the run
        ctx.db.rollback()
        logger.exception("Generation batch failed")
        log_error(
            ctx.db,
            source="generation",
            message=str(exc),
            context={"subspecialty": step["subspecialty"], "count": step["count"]},
        )
        failures = int((ctx.job.result or {}).get("failed_batches", 0)) + 1
        ctx.set_result(failed_batches=failures)

    ctx.cursor_set(position=position + 1)
    made = len((ctx.job.result or {}).get("created", []))
    ctx.advance(1, f"Generated {made} of {target} {question_type}(s)")
    return position + 1 >= len(plan)


# --- Helpers --------------------------------------------------------------
def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def _marks(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _angoff(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None
