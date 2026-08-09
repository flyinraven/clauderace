"""What a full-mark answer actually says, for every rubric point.

A rubric point is written for an examiner holding a pen: "Identifies the
inferior corneal thinning" tells them what to tick. It does not tell a
candidate reading their result what they should have said, and the grading
comment only says why the mark was withheld - "did not mention the thinning" -
which is the same sentence again.

So the review could say a point was missed and never say what filling it would
have sounded like. This writes that: the words a candidate would speak to earn
the point, in their voice, from the station's own findings and diagnosis.

**It belongs to the question, not to the sitting.** The answer to "what does
this fundus show" does not depend on who was asked, so it is written once per
station and read by every review of it - including the ones already sat, which
is why the review joins it in by index rather than copying it into each grade.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_MODEL_ANSWERS = "model_answer_osce_stations"

SYSTEM_PROMPT = """\
You are a RANZCO examiner writing the model answer for an OSCE station, for a \
candidate reading their result afterwards.

For each marking point you are given, write what a candidate would SAY to earn \
it in full. Not a description of the point - the answer itself, in the words \
someone would speak at the station.

  point:  Identifies the inferior corneal thinning
  answer: "There is thinning of the inferior cornea, about two thirds depth, \
with the thinnest point below the visual axis."

Write one or two sentences for each. Be specific: name the finding, its \
location, and the detail that distinguishes it. A model answer that could be \
given at any station is worth nothing to the person reading it.

Take the content from the station's findings, diagnosis and history. Where they \
are silent, say what this patient's confirmed diagnosis would have the \
candidate say. Never invent a measurement the station does not record.

This is read AFTER the candidate has been marked, so naming the diagnosis is \
correct here - it is the answer, and withholding it would defeat the point.

Return ONLY a JSON object mapping each index, as a string, to its answer:
{"0": "...", "1": "..."}"""


def _points_missing_answers(prompt: dict[str, Any]) -> list[int]:
    """The rubric indices with no model answer yet."""
    return [
        index
        for index, point in enumerate(prompt.get("rubric") or [])
        if not str(point.get("model_answer") or "").strip()
    ]


def stations_needing_model_answers(db: Session) -> list[int]:
    """Stations with a marking point nobody has written the answer to.

    Already-written answers are skipped, so a run can be repeated after a
    re-marking round without paying for the whole bank again.
    """
    out = []
    for station_id, prompts in db.execute(
        select(OsceStation.id, OsceStation.prompts)
    ).all():
        if any(_points_missing_answers(p) for p in prompts or []):
            out.append(station_id)
    return out


def write_model_answers(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Write the answer to every marking point on one station. One call."""
    prompts = [dict(p) for p in (station.prompts or [])]
    if not prompts:
        return {"skipped": 1}

    # Indexed across the whole station, so one call covers it. Numbering per
    # question meant six sets of "0, 1, 2" in one reply and answers landing on
    # the wrong question.
    wanted: list[tuple[int, int, dict[str, Any]]] = []
    for prompt_index, prompt in enumerate(prompts):
        for point_index in _points_missing_answers(prompt):
            wanted.append((prompt_index, point_index, prompt["rubric"][point_index]))
    if not wanted:
        return {"already_written": 1}

    listing = "\n".join(
        f"  {n} | [{prompts[pi].get('label')}] asked: {prompts[pi].get('text')}\n"
        f"      point: {point.get('text')}"
        for n, (pi, _qi, point) in enumerate(wanted)
    )
    user = (
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n"
        f"DIAGNOSIS: {station.diagnosis or 'not recorded'}\n"
        f"CASE: {station.case_summary or '(none)'}\n"
        f"HISTORY: {station.patient_history or '(none)'}\n"
        f"FINDINGS:\n{station.findings_elicited or station.findings or '(none)'}\n\n"
        f"MISTAKES THE EXAMINERS NOTED:\n{station.common_mistakes or '(none)'}\n\n"
        f"THE MARKING POINTS, numbered:\n{listing}\n\n"
        f"Write the model answer for each numbered point."
    )
    data = client.complete_json(
        task="model_answer", system=SYSTEM_PROMPT, user=user, job_id=job_id,
        max_tokens=2400, temperature=0.2,
    )
    if not isinstance(data, dict):
        raise ValueError("Model answers did not come back as a JSON object")

    written = 0
    for n, (prompt_index, point_index, _point) in enumerate(wanted):
        answer = str(data.get(str(n)) or data.get(n) or "").strip()
        if not answer:
            continue
        prompts[prompt_index]["rubric"][point_index]["model_answer"] = answer
        written += 1

    if not written:
        return {"rejected": 1, "reason": "no answers were written for any point"}

    station.prompts = prompts
    flag_modified(station, "prompts")
    db.commit()
    return {"written": written, "stations": 1, "left": len(wanted) - written}


@register_handler(JOB_MODEL_ANSWERS)
def handle_model_answers(ctx: JobContext) -> bool:
    """One station per chunk, one model call each."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True
    if ctx.cancelled:
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            outcome = write_model_answers(
                ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id
            )
        except Exception as exc:  # noqa: BLE001 - one station must not stop the run
            ctx.db.rollback()
            logger.exception("Could not write model answers for station %s", station.id)
            log_error(ctx.db, source="osce_model_answers", message=str(exc),
                      context={"station_id": station.id})
            outcome = {"failed": 1}

        if outcome.get("rejected"):
            log_error(
                ctx.db,
                source="osce_model_answers",
                level="warning",
                message=f"Station {station.id} left as it was: {outcome.get('reason')}",
                context={"station_id": station.id},
            )

        running = dict(ctx.job.result or {})
        for key, value in outcome.items():
            if isinstance(value, int):
                running[key] = running.get(key, 0) + value
        ctx.set_result(**running)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Model answers: {index + 1} of {len(station_ids)} stations")
    return index + 1 >= len(station_ids)
