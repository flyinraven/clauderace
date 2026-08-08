"""Give marks to the questions that carry none.

The prompt builder is told the marks must total 20 and that is checked. Nothing
said every question must be worth some, so the model concentrated them: 147
questions across 98 stations ended up worth nothing - one station with three of
its six - and the marker replies "This question carries no marks" to an answer
that took a minute of a nine-minute station to give. 154 minutes of clock time
across the bank buys nothing.

The rule is now enforced where prompts are built, so this is for the stations
built before it.

**Only the marks move.** The question text is left exactly as it is. Those
stems were rewritten twice on 7-8 August - trimmed to the images that actually
arrived, and made to ask for a differential before the diagnosis is revealed -
and rebuilding the questions would discard all of it and change what images
each one wants, which would mean sourcing the bank again. A rebuild is the
expensive way to fix an arithmetic problem.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import OsceStation
from app.services.ai import AIClient
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_REMARK_STATIONS = "remark_osce_stations"

STATION_MARKS = 20.0

SYSTEM_PROMPT = """\
You are a RANZCO examiner repairing the marking key of one OSCE station.

Every question is already written and must NOT be changed. Some carry no marks \
at all, which means the candidate answers them for nothing. Your job is to give \
those questions a marking key, and to take the marks for them from the \
questions that have more than their share.

Rules:
- The station totals exactly 20 marks. It must still total exactly 20.
- EVERY question must end up worth at least 1 mark.
- Do not change any question's text.
- Write the new rubric points from what the question actually asks and from the \
station's recorded findings and diagnosis. Each point is something the \
candidate says, marked present or absent - "Identifies the inferior corneal \
thinning", not "Understands keratoconus".
- Keep the existing rubric points of the questions that already have them, \
reducing their marks where you must rather than deleting them. A point that \
survives with fewer marks is still a point the candidate is credited for; a \
deleted one is a thing they said and were not.
- Marks are whole numbers or halves. Give the longer, harder questions more.

Return ONLY a JSON object mapping every question's label to its rubric:
{
  "A": [{"text": "...", "marks": 4, "is_critical": true}, ...],
  "B": [...]
}"""


def stations_needing_marks(db: Session) -> list[int]:
    """Stations with at least one question worth nothing."""
    out = []
    for station_id, prompts in db.execute(
        select(OsceStation.id, OsceStation.prompts)
    ).all():
        for prompt in prompts or []:
            rubric = prompt.get("rubric") or []
            if sum(as_float(pt.get("marks"), 0.0) for pt in rubric) <= 0:
                out.append(station_id)
                break
    return out


def _total(prompts: list[dict[str, Any]]) -> float:
    return sum(
        as_float(pt.get("marks"), 0.0)
        for p in prompts
        for pt in (p.get("rubric") or [])
    )


def remark_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Redistribute one station's 20 marks so every question carries some."""
    prompts = [dict(p) for p in (station.prompts or [])]
    if not prompts:
        return {"skipped": 1}
    unmarked = [
        p.get("label")
        for p in prompts
        if sum(as_float(pt.get("marks"), 0.0) for pt in (p.get("rubric") or [])) <= 0
    ]
    if not unmarked:
        return {"already_marked": 1}

    listing = "\n\n".join(
        f"[{p.get('label')}] ({sum(as_float(pt.get('marks'), 0.0) for pt in (p.get('rubric') or [])):g} marks now)"
        f" {p.get('text')}\n"
        + "\n".join(
            f"    - ({as_float(pt.get('marks'), 0.0):g}) {pt.get('text')}"
            for pt in (p.get("rubric") or [])
        )
        for p in prompts
    )
    user = (
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n"
        f"DIAGNOSIS: {station.diagnosis or 'not recorded'}\n"
        f"FINDINGS:\n{station.findings_elicited or station.findings or '(none)'}\n\n"
        f"MISTAKES THE EXAMINERS NOTED:\n{station.common_mistakes or '(none)'}\n\n"
        f"THE QUESTIONS AND THEIR MARKS AS THEY STAND:\n{listing}\n\n"
        f"Questions carrying no marks: {', '.join(str(u) for u in unmarked)}\n"
        f"Return the full marking key for every question."
    )
    data = client.complete_json(
        task="utility", system=SYSTEM_PROMPT, user=user, job_id=job_id
    )
    if not isinstance(data, dict):
        raise ValueError("Re-marking did not return a JSON object")

    rebuilt = []
    for prompt in prompts:
        points = data.get(str(prompt.get("label")))
        if isinstance(points, list) and points:
            prompt["rubric"] = [
                {
                    "text": str(pt.get("text") or "").strip(),
                    "marks": as_float(pt.get("marks"), 0.0),
                    "is_critical": bool(pt.get("is_critical")),
                }
                for pt in points
                if str(pt.get("text") or "").strip()
            ]
        rebuilt.append(prompt)

    still_unmarked = [
        p.get("label")
        for p in rebuilt
        if sum(as_float(pt.get("marks"), 0.0) for pt in (p.get("rubric") or [])) <= 0
    ]
    total = _total(rebuilt)
    if still_unmarked or abs(total - STATION_MARKS) > 0.01:
        # Refused rather than saved. A station that no longer totals 20 marks
        # every candidate against a different maximum, and one still carrying a
        # dead question has not been repaired - either is worse than the
        # station as it stands, which at least is understood.
        return {
            "rejected": 1,
            "reason": f"totals {total:g}, unmarked: {still_unmarked or 'none'}",
        }

    station.prompts = rebuilt
    flag_modified(station, "prompts")
    station.total_marks = int(STATION_MARKS)
    db.commit()
    return {"remarked": 1, "questions_given_marks": len(unmarked)}


@register_handler(JOB_REMARK_STATIONS)
def handle_remark_stations(ctx: JobContext) -> bool:
    """One station per chunk."""
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
            outcome = remark_station(ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the run
            ctx.db.rollback()
            logger.exception("Could not re-mark station %s", station.id)
            log_error(ctx.db, source="osce_remark", message=str(exc),
                      context={"station_id": station.id})
            outcome = {"failed": 1}
        running = dict(ctx.job.result or {})
        for key, value in outcome.items():
            if isinstance(value, int):
                running[key] = running.get(key, 0) + value
        ctx.set_result(**running)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Re-marked {index + 1} of {len(station_ids)} stations")
    return index + 1 >= len(station_ids)
