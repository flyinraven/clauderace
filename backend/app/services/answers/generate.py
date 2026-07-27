"""Model answer generation.

This is the feature that makes the platform worth using. The RANZCO examiners'
reports publish the questions and, crucially, what the cohort got wrong - but
never the answers. Feeding that examiner commentary back into answer generation
produces a marking key calibrated to what examiners actually rewarded, rather
than a generic textbook summary.

Any figures attached to the question are sent to a vision model in the same
call, so answers to "describe the findings on this OCT" are grounded in the
actual image rather than guessed from the stem.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Figure, Image, ModelAnswerPoint, Question, QuestionPart
from app.services.ai import AIClient, ImagePart, TextPart
from app.services.coerce import as_float, as_int, as_optional_float, clean_str
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.marking import absorb_mark_drift

logger = logging.getLogger(__name__)

JOB_GENERATE_MODEL_ANSWERS = "generate_model_answers"

MAX_IMAGES_PER_QUESTION = 6

SYSTEM_PROMPT = """\
You are a senior RANZCO examiner writing the official marking key for a RACE \
(RANZCO Advanced Clinical Examination) written paper. Your reader is a \
consultant-level examiner marking real candidates, and the standard expected is \
that of an ophthalmologist ready for independent practice in Australia or \
New Zealand.

For every sub-question, produce the discrete, markable key points an examiner \
would tick off.

Non-negotiable rules:
1. The marks you assign across a sub-question's key points must sum EXACTLY to \
that sub-question's mark allocation. If a part is worth 5 marks, your points \
must total 5 - typically five 1-mark points, but 1.5/0.5 splits are acceptable \
where the content warrants it.
2. If asked for a specific number of items ("List five (5) risk factors"), give \
exactly that many key points.
3. Each key point is ONE markable idea, stated the way a candidate would write \
it under time pressure - concise and clinical, not an essay. Aim for one line.
4. Where a point has legitimate synonyms or equivalent answers an examiner would \
accept, list them in "accepted_alternatives".
5. EXAMINER FEEDBACK IS AUTHORITATIVE. Where the report says candidates missed \
something, that content MUST appear as a key point, with \
"from_examiner_feedback": true and "is_critical": true. Where the report says a \
common answer was wrong, do not include that wrong answer, and note the trap in \
"rationale". Where examiners said a topic was well answered, still include it.
6. Reflect ANZ practice: Medicare/PBS context, RANZCO guidelines, and the \
relevant landmark trials by name where an examiner would expect them.
7. Do not invent findings that are not supported by the stem or the images. If \
the question shows an image, describe what is actually visible.

Return ONLY a JSON object:
{
  "parts": [
    {
      "part_id": <integer, exactly as given>,
      "key_points": [
        {
          "text": "the markable point",
          "marks": <number>,
          "is_critical": <boolean>,
          "from_examiner_feedback": <boolean>,
          "rationale": "why this earns marks, or the trap to avoid" | null,
          "accepted_alternatives": [string]
        }
      ]
    }
  ],
  "figure_descriptions": [
    {"label": "Figure 1", "description": "clinical description of what is shown"}
  ],
  "angoff_expected": <number 0-1: the fraction of total marks a borderline
                      candidate - one just at the pass standard - would score>,
  "angoff_rationale": "one sentence justifying that estimate",
  "examiner_note": "one short paragraph of overall guidance for this question"
}"""


def _build_prompt(question: Question, parts: list[QuestionPart], figures: list[tuple[Figure, Image | None]]) -> list:
    lines: list[str] = []
    lines.append(f"QUESTION TYPE: {question.question_type} (total {question.total_marks} marks)")
    if question.topic:
        lines.append(f"TOPIC: {question.topic}")
    if question.subspecialty:
        lines.append(f"SUBSPECIALTY: {question.subspecialty}")
    if question.curriculum_standard_raw:
        lines.append(f"CURRICULUM STANDARD: {question.curriculum_standard_raw}")
    if question.purpose:
        lines.append(f"PURPOSE OF THE QUESTION (as stated by the examiners): {question.purpose}")

    lines.append("\nCLINICAL STEM:\n" + (question.stem or "(none)"))

    if figures:
        lines.append("\nFIGURES (images attached below, in this order):")
        for figure, _image in figures:
            caption = f" - {figure.caption}" if figure.caption else ""
            lines.append(f"  {figure.label or 'Figure'}{caption}")

    lines.append("\nSUB-QUESTIONS:")
    for part in parts:
        if part.preamble:
            lines.append(f"\n  [scenario continues] {part.preamble}")
        label = f"{part.label}) " if part.label else ""
        lines.append(f"  part_id={part.id} | {label}{part.text} ({part.marks:g} marks)")

    feedback_lines = _format_feedback(question)
    if feedback_lines:
        lines.append(
            "\nEXAMINERS' REPORT FOR THIS EXACT QUESTION - what the real cohort "
            "did and did not do. Treat this as authoritative:"
        )
        lines.extend(feedback_lines)
    else:
        lines.append("\n(No examiner commentary was published for this question.)")

    lines.append(
        "\nWrite the marking key now. Check your arithmetic: each part's key "
        "point marks must sum exactly to that part's allocation."
    )

    content: list = [TextPart("\n".join(lines))]
    for _figure, image in figures[:MAX_IMAGES_PER_QUESTION]:
        if image and image.data:
            content.append(ImagePart(data=image.data, media_type=image.content_type))
    return content


def _format_feedback(question: Question) -> list[str]:
    out: list[str] = []
    for feedback in question.examiner_feedback:
        who = f"Examiner #{feedback.examiner_number}" if feedback.examiner_number else "Examiner"
        for item in feedback.common_mistakes or []:
            out.append(f"  - [{who} - common mistake] {item}")
        for item in feedback.cohort_impression or []:
            out.append(f"  - [{who} - impression of the cohort] {item}")
    return out


def generate_model_answer(
    db: Session, client: AIClient, question: Question, job_id: int | None = None
) -> dict[str, Any]:
    """Generate and persist the marking key for one question."""
    parts = sorted(question.parts, key=lambda p: p.position)
    if not parts:
        raise ValueError("Question has no sub-questions to answer")

    figures: list[tuple[Figure, Image | None]] = []
    for figure in sorted(question.figures, key=lambda f: f.position):
        image = db.get(Image, figure.image_id) if figure.image_id else None
        figures.append((figure, image))

    content = _build_prompt(question, parts, figures)

    # Budget output by question size. A 20-mark SEQ with seven sub-questions
    # needs several times the room of a 2-mark VSAQ, and a key truncated at the
    # limit comes back as unparseable JSON rather than a partial answer.
    budget = max(4000, 1500 * len(parts) + 400 * int(question.total_marks or 0))
    data = client.complete_json(
        task="model_answer", system=SYSTEM_PROMPT, user=content,
        max_tokens=min(budget, 32000), job_id=job_id,
    )
    if not isinstance(data, dict):
        raise ValueError("Model answer response was not a JSON object")

    parts_by_id = {p.id: p for p in parts}
    warnings: list[str] = []
    written = 0

    # Replace any previous key, so regenerating is idempotent.
    for part in parts:
        db.execute(
            ModelAnswerPoint.__table__.delete().where(ModelAnswerPoint.part_id == part.id)
        )

    for spec in data.get("parts") or []:
        if not isinstance(spec, dict):
            continue
        part = parts_by_id.get(as_int(spec.get("part_id")))
        if part is None:
            warnings.append(f"Model returned an unknown part_id {spec.get('part_id')!r}")
            continue

        points = [p for p in (spec.get("key_points") or []) if isinstance(p, dict) and p.get("text")]
        if not points:
            warnings.append(f"No key points returned for part {part.label or part.position + 1}")
            continue

        total = sum(as_float(p.get("marks"), 0.0) for p in points)
        if abs(total - float(part.marks)) > 0.01:
            # Rescale rather than discard: the content is usually right even when
            # the arithmetic drifts, and an examiner-facing warning is recorded.
            warnings.append(
                f"Part {part.label or part.position + 1}: key points totalled "
                f"{total:g} of {part.marks:g} marks; rescaled."
            )
            if total > 0:
                factor = float(part.marks) / total
                for point in points:
                    point["marks"] = round(as_float(point.get("marks"), 0.0) * factor, 2)
            else:
                even = float(part.marks) / len(points)
                for point in points:
                    point["marks"] = round(even, 2)
            absorb_mark_drift(points, float(part.marks))

        for position, point in enumerate(points):
            db.add(
                ModelAnswerPoint(
                    part_id=part.id,
                    position=position,
                    text=str(point["text"]).strip(),
                    marks=as_float(point.get("marks"), 0.0),
                    is_critical=bool(point.get("is_critical")),
                    from_examiner_feedback=bool(point.get("from_examiner_feedback")),
                    rationale=clean_str(point.get("rationale")),
                    accepted_alternatives=[
                        str(a).strip()
                        for a in (point.get("accepted_alternatives") or [])
                        if str(a).strip()
                    ]
                    or None,
                )
            )
            written += 1

    _store_figure_descriptions(db, figures, data.get("figure_descriptions") or [])

    angoff = as_optional_float(data.get("angoff_expected"))
    if angoff is not None and 0 <= angoff <= 1:
        question.angoff_expected = angoff
        question.angoff_rationale = clean_str(data.get("angoff_rationale"))

    meta = dict(question.generation_meta or {})
    meta["model_answer_warnings"] = warnings
    if data.get("examiner_note"):
        meta["examiner_note"] = str(data["examiner_note"]).strip()
    question.generation_meta = meta
    question.model_answer_status = "complete" if written else "failed"

    db.commit()
    return {"points": written, "warnings": warnings}



def _store_figure_descriptions(
    db: Session, figures: list[tuple[Figure, Image | None]], descriptions: list[Any]
) -> None:
    by_label = {
        (f.label or "").strip().lower(): image for f, image in figures if image is not None
    }
    for index, spec in enumerate(descriptions):
        if not isinstance(spec, dict):
            continue
        text = clean_str(spec.get("description"))
        if not text:
            continue
        label = str(spec.get("label") or "").strip().lower()
        image = by_label.get(label)
        if image is None and index < len(figures):
            image = figures[index][1]
        if image is not None and not image.ai_description:
            image.ai_description = text


# --- Job handler ----------------------------------------------------------
@register_handler(JOB_GENERATE_MODEL_ANSWERS)
def handle_generate_model_answers(ctx: JobContext) -> bool:
    """Generate answers for a list of questions, one per chunk."""
    question_ids: list[int] = ctx.payload.get("question_ids") or []
    if not question_ids:
        raise JobHandlerError("No question_ids supplied")

    if not ctx.job.total_steps:
        ctx.set_total(len(question_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(question_ids):
        return True

    question_id = question_ids[index]
    question = ctx.db.get(Question, question_id)
    client = AIClient(ctx.db)

    if question is None:
        ctx.set_result(**{"missing": list((ctx.job.result or {}).get("missing", [])) + [question_id]})
    else:
        try:
            outcome = generate_model_answer(ctx.db, client, question, job_id=ctx.job.id)
            done = list((ctx.job.result or {}).get("completed", []))
            done.append(question_id)
            ctx.set_result(completed=done)
            if outcome["warnings"]:
                warned = list((ctx.job.result or {}).get("warnings", []))
                warned.extend(f"Q{question_id}: {w}" for w in outcome["warnings"])
                ctx.set_result(warnings=warned)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
            logger.exception("Model answer generation failed for question %s", question_id)
            log_error(
                ctx.db,
                source="model_answer",
                message=f"Question {question_id}: {exc}",
                context={"question_id": question_id},
            )
            question.model_answer_status = "failed"
            ctx.db.commit()
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(question_id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Model answers: {index + 1} of {len(question_ids)}")
    return index + 1 >= len(question_ids)


def questions_needing_answers(db: Session, limit: int | None = None) -> list[int]:
    stmt = (
        select(Question.id)
        .where(Question.model_answer_status.in_(["none", "failed"]))
        .order_by(Question.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


