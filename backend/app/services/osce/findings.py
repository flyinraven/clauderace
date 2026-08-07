"""Split a station's findings into what is given and what must be elicited.

At a real OSCE the examiner hands the candidate the numbers a technician would
already have measured - visual acuity, intraocular pressure, refraction - and
then expects them to find the clinical signs themselves. Showing the whole
`findings` block up front gives away the answer to every "describe what you
see" prompt, so it is separated and only the given half is shown during the
sitting.
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

JOB_SPLIT_OSCE_FINDINGS = "split_osce_findings"

SYSTEM_PROMPT = """\
You are preparing a RANZCO RACE OSCE station. You are given the station's raw \
examination findings as printed in the examiners' report.

Split them into two groups, exactly as a real OSCE works:

GIVEN - what the examiner states to the candidate at the start, because it was \
measured before they walked in and cannot be obtained by looking:
  - visual acuity (aided and unaided)
  - intraocular pressure
  - refraction
  - any explicitly stated investigation result the candidate cannot perform \
themselves (e.g. a reported field defect, an imaging report)

ELICITED - the clinical signs the candidate is expected to find and describe \
themselves, which must NOT be shown to them in advance:
  - anything visible on examination: lens or corneal appearance, iris changes, \
disc appearance, motility deficits, lid position, proptosis, dystopia
  - the presence, character and position of any lesion
  - relative afferent pupillary defect and other bedside test results

Rules:
- Copy the wording across; do not paraphrase away clinical detail or numbers.
- Every piece of the original findings must land in exactly one group.
- Use ONLY the raw findings given below. Nothing may appear in either group that is not in them. You are dividing a text, not writing one.
- GIVEN must NEVER contain the diagnosis, the name of the disease, or any conclusion drawn from the findings, however it is phrased. "The patient presents with bilateral Brown's syndrome" and "glaucomatous optic neuropathy is present" are diagnoses wearing the clothes of a handed-over result. Keep the measurement - "IOP 25 mmHg", "central field loss in the right eye" - and leave the name of the disease out. A candidate told the diagnosis has nothing left to work out, and every diagnostic mark on the station becomes free.
- If a line is ambiguous, put it in ELICITED. Withholding something a candidate \
would have been told is a small unfairness; revealing a sign they were supposed \
to find destroys the station.
- If there are genuinely no findings of a type, use an empty string.

Return ONLY a JSON object:
{
  "given": "the findings the examiner states, as readable lines",
  "elicited": "the signs the candidate must find, as readable lines"
}"""


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

# The words the measurements themselves are made of. A diagnosis sharing one of
# these must not be grounds for withholding the line that carries it:
# "Cortical visual impairment" would otherwise take "Visual acuity 6/60" out of
# the stem, which is the very thing the stem exists to hand over.
_STEM_VOCABULARY = frozenset(
    """visual vision acuity aided unaided corrected pinhole refraction
    intraocular pressure iop mmhg goldmann applanation eye eyes right left
    both near distance""".split()
)


def withhold_diagnosis(given: str, station: OsceStation) -> tuple[str, list[str]]:
    """Move any sentence of GIVEN that names the diagnosis into what must be found.

    The prompt forbids this and the model still did it - station 156 opened
    with "The patient presents with bilateral Brown's Syndrome" beside the
    visual acuity, so every diagnostic mark on the station was free before the
    candidate had looked at anything. A rule that only lives in a prompt is a
    request; this is the check.

    Matched on the distinctive words of the diagnosis rather than the whole
    phrase, so "advanced glaucoma and maximally tolerated therapy" is caught
    when the diagnosis is "primary open angle glaucoma". Generic words are
    stripped first, or "Visual acuity 6/6" would be withheld from a station
    whose diagnosis contains the word "visual".
    """
    from app.services.osce.station_images.verify import _GENERIC_WORDS, _words

    distinctive = _words(station.diagnosis or "") - _GENERIC_WORDS - _STEM_VOCABULARY
    if not distinctive or not given.strip():
        return given, []

    kept: list[str] = []
    moved: list[str] = []
    for sentence in _SENTENCE.split(given):
        if not sentence.strip():
            continue
        if _words(sentence) & distinctive:
            moved.append(sentence.strip())
        else:
            kept.append(sentence.strip())
    return (" ".join(kept), moved) if moved else (given, [])


def split_findings(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    if not (station.findings or "").strip():
        station.findings_given = None
        station.findings_elicited = None
        station.findings_split_status = "complete"
        db.commit()
        return {"given": 0, "elicited": 0}

    user = (
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n"
        f"CASE: {station.case_summary or '(none)'}\n\n"
        f"RAW FINDINGS AS PRINTED:\n{station.findings}\n\n"
        f"Split them now."
    )
    data = client.complete_json(
        task="utility", system=SYSTEM_PROMPT, user=user, job_id=job_id
    )
    if not isinstance(data, dict):
        raise ValueError("Findings split did not return a JSON object")

    given = str(data.get("given") or "").strip()
    elicited = str(data.get("elicited") or "").strip()

    given, withheld = withhold_diagnosis(given, station)
    if withheld:
        # Not discarded: it is a real finding, just one the candidate is meant
        # to reach rather than be handed. Elicited is never shown before the
        # result, so this is where it belongs.
        elicited = "\n".join(filter(None, [elicited, *withheld]))
        logger.warning(
            "Station %s: withheld %d line(s) naming the diagnosis from the stem",
            station.id, len(withheld),
        )

    station.findings_given = given or None
    station.findings_elicited = elicited or None
    station.findings_split_status = "complete"
    db.commit()
    return {"given": len(given), "elicited": len(elicited)}


@register_handler(JOB_SPLIT_OSCE_FINDINGS)
def handle_split_osce_findings(ctx: JobContext) -> bool:
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")

    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            split_findings(ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id)
            done = list((ctx.job.result or {}).get("completed", []))
            done.append(station.id)
            ctx.set_result(completed=done)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the batch
            ctx.db.rollback()
            logger.exception("Findings split failed for station %s", station.id)
            log_error(
                ctx.db, source="osce_findings", message=str(exc),
                context={"station_id": station.id},
            )
            station.findings_split_status = "failed"
            ctx.db.commit()
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(station.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Findings split: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


def stations_needing_split(db: Session) -> list[int]:
    return list(
        db.execute(
            select(OsceStation.id)
            .where(OsceStation.findings_split_status.in_(["none", "failed"]))
            .order_by(OsceStation.id)
        ).scalars().all()
    )
