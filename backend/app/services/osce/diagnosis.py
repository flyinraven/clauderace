"""Name the condition a station records everywhere except its diagnosis field.

Eleven stations have no diagnosis. Not because the report withheld one - block
2A of 2020 Semester 2 has no DIAGNOSIS heading at all - but because it is
stated in the SUMMARY OF CASE instead: "An infant with pseudoesotropia", "a
patient with orbital inflammatory disease", "a 24-year-old female with
congenital toxoplasmosis".

Two things break when that field is empty, and neither announces itself:

  * `leaked_term` matches a description against the diagnosis, so an empty
    diagnosis passes everything. The background block and the stated findings
    on those stations could name the condition outright and nothing would
    notice - the guard is not lenient there, it is absent.
  * `describe_findings` describes from the findings or the diagnosis. Station
    270 has neither, so its representative image - which the vision model says
    does not show the eyes in different positions of gaze - has no words beside
    it, and the marks behind it cannot be earned.

Extraction, not diagnosis: the model is given the case summary and asked which
condition it names, told to return nothing if it names none, and its answer is
checked against the summary before it is kept.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_RECOVER_DIAGNOSES = "recover_osce_diagnoses"

SYSTEM_PROMPT = """\
You are reading the case summary of a RANZCO OSCE station and naming the \
condition it is about.

Return the condition exactly as the summary words it, with no added qualifiers, \
laterality or severity that the summary does not state. "An infant with \
pseudoesotropia" gives "Pseudoesotropia". "A 24-year-old female with congenital \
toxoplasmosis presents with a right macular scar" gives "Congenital \
toxoplasmosis".

If the summary describes a presentation without naming a condition - "a painful \
red eye for one day" - return an empty string. Do not diagnose: you are quoting \
what is already written, not working it out.

Return ONLY a JSON object: {"diagnosis": "..."}"""


def stations_missing_a_diagnosis(db: Session) -> list[int]:
    """Stations with no diagnosis but a case summary that might name one."""
    return sorted(
        s.id
        for s in db.execute(select(OsceStation)).scalars()
        if not (s.diagnosis or "").strip() and (s.case_summary or "").strip()
    )


def name_the_condition(client: AIClient, station: OsceStation) -> str | None:
    """The condition this station's summary names, or None if it names none."""
    data = client.complete_json(
        task="utility",
        system=SYSTEM_PROMPT,
        user=f"SUMMARY OF CASE:\n{station.case_summary}",
    )
    text = str((data or {}).get("diagnosis") or "").strip()
    # A summary naming no condition keeps its empty field. A guess would be
    # worse than nothing: the leak guard would start withholding lines for a
    # diagnosis nobody ever recorded.
    if not text or len(text) > 120:
        return None
    # The instruction says quote, and this is the check - the words have to be
    # the station's own, not the model's.
    words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
    if words and not any(w in (station.case_summary or "").lower() for w in words):
        logger.info("Station %s: %r is not in its summary, so it is left alone",
                    station.id, text)
        return None
    return text


@register_handler(JOB_RECOVER_DIAGNOSES)
def handle_recover_diagnoses(ctx: JobContext) -> bool:
    """One station per chunk, one utility call each."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None and not (station.diagnosis or "").strip():
        try:
            named = name_the_condition(AIClient(ctx.db), station)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the run
            ctx.db.rollback()
            logger.exception("Could not read a diagnosis for station %s", station.id)
            log_error(ctx.db, source="osce_diagnosis", message=str(exc),
                      context={"station_id": station.id})
            named = None
        key = "named" if named else "silent"
        if named:
            station.diagnosis = named
        done: list[Any] = list((ctx.job.result or {}).get(key, []))
        done.append(station.id)
        ctx.set_result(**{key: done})
        ctx.db.commit()

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Diagnoses: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)
