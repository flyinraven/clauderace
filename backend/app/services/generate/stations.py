"""Generate new OSCE stations.

The examiners' reports supplied 36 stations, but Ocular Motility has only two -
enough for two days of a nine-station daily circuit before it starts repeating
cases the candidate already knows the answer to. That teaches recall of a
specific patient rather than clinical reasoning, so the bank needs topping up.

A station is produced complete in one call: the case, the findings split into
what an examiner states versus what must be elicited, the diagnosis, and the
timed sequence of spoken examiner questions with the 20-mark rubric divided
across them. Generating the parts separately would let them drift apart.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import (
    OSCE_STATION_MINUTES,
    SOURCE_GENERATED,
    STATUS_REVIEW,
    SUBSPECIALTIES,
    normalise_subspecialty,
)
from app.models import OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import (
    JobContext,
    JobHandlerError,
    create_job,
    register_handler,
)
from app.services.osce.prompts import STATION_SECONDS, _normalise as normalise_prompts

logger = logging.getLogger(__name__)

JOB_GENERATE_STATIONS = "generate_osce_stations"

SYSTEM_PROMPT = f"""\
You are a RANZCO examiner writing a new station for the RACE OSCE. Candidates \
rotate through 18 stations of {OSCE_STATION_MINUTES} minutes each, examining a \
real patient and answering the examiner aloud.

Write one station based on a patient a RANZCO examiner would realistically \
recruit: a stable, chronic or post-operative case with visible, describable \
signs. Not an emergency, not a patient who would be too unwell to sit through \
a morning of candidates.

Structure it exactly as a real station works:

- "findings_given" is what the examiner states at the outset because it was \
measured beforehand: visual acuity in Snellen 6/x notation, intraocular \
pressure, refraction, and any investigation the candidate cannot perform \
themselves.
- "findings_elicited" is what the candidate must find and describe: the lens, \
cornea, iris, disc, motility, lids, proptosis. This is the answer to the \
opening question, so it must NOT appear in findings_given.
- "prompts" is the spoken sequence: {3} to {7} questions in the order an \
examiner asks them - describe the findings, then interpret, then differential, \
then investigation and management, then complications or counselling.
- Each prompt carries its own slice of the rubric. Marks across ALL prompts \
must total exactly 20. Seconds across all prompts must total exactly \
{STATION_SECONDS}.
- Word each question as you would say it aloud: short and direct.
- "common_mistakes" are the errors you would expect this station to expose in \
a real cohort - be specific and clinically grounded.

Standard: a trainee ready for independent consultant practice in Australia or \
New Zealand. Use ANZ terminology and name landmark trials where an examiner \
would expect them. Every measurement must be internally consistent - the \
acuity, IOP and signs should describe one coherent patient.

Return ONLY a JSON object:
{{
  "title": "short descriptive title, naming the pathology",
  "subspecialty": "one of the nine",
  "case_summary": "one or two sentences of what the station presents",
  "aims": [string],
  "patient_demographic": "all the candidate sees before starting - age band and
                          sex only, e.g. 'An elderly woman', 'A young boy'.
                          It must NOT hint at the diagnosis",
  "patient_history": "age, sex, presenting history and relevant background",
  "findings_given": "the measurements the examiner states",
  "findings_elicited": "the signs the candidate must find",
  "diagnosis": "the diagnosis",
  "common_mistakes": [string],
  "angoff_expected": <0-1, the fraction of 20 marks a borderline candidate scores>,
  "image_needed": "the clinical image this station should show",
  "prompts": [
    {{"label": "A", "text": "the question as spoken", "seconds": <integer>,
      "rubric": [{{"text": "markable expectation", "marks": <number>,
                   "is_critical": <boolean>}}]}}
  ]
}}"""


def _existing_titles(db: Session, subspecialty: str | None) -> list[str]:
    stmt = select(OsceStation.title, OsceStation.diagnosis)
    if subspecialty:
        stmt = stmt.where(OsceStation.subspecialty == subspecialty)
    out = []
    for title, diagnosis in db.execute(stmt).all():
        out.append(title or diagnosis or "")
    return [x for x in out if x]


def _style_examples(db: Session, subspecialty: str | None, limit: int = 1) -> str:
    """A real ingested station, for tone and level."""
    stmt = (
        select(OsceStation)
        .where(OsceStation.source != SOURCE_GENERATED)
        .where(OsceStation.prompts_status == "complete")
        .order_by(func.random())
        .limit(limit)
    )
    if subspecialty:
        stmt = stmt.where(OsceStation.subspecialty == subspecialty)
    stations = db.execute(stmt).scalars().all()
    if not stations:
        return ""

    blocks = []
    for s in stations:
        prompts = "\n".join(
            f"    {p.get('label')} [{p.get('seconds')}s] {p.get('text')}"
            for p in (s.prompts or [])
        )
        blocks.append(
            f"CASE: {s.case_summary}\nHISTORY: {s.patient_history}\n"
            f"FINDINGS: {s.findings}\nDIAGNOSIS: {s.diagnosis}\n"
            f"QUESTIONS:\n{prompts}"
        )
    return (
        "\n\nA real RANZCO station, for tone and level only - do NOT reuse its "
        "content:\n\n" + "\n\n---\n\n".join(blocks)
    )


def generate_station(
    db: Session,
    client: AIClient,
    subspecialty: str | None,
    difficulty: str | None = None,
    job_id: int | None = None,
) -> int | None:
    """Generate and persist one station. Returns its id, or None if rejected."""
    avoid = _existing_titles(db, subspecialty)
    random.shuffle(avoid)

    lines = [
        f"Write one OSCE station for: {subspecialty or 'any of the nine subspecialties'}.",
        f"The nine subspecialties are: {', '.join(SUBSPECIALTIES)}.",
    ]
    if difficulty:
        lines.append(f"Target difficulty: {difficulty}.")
    if avoid:
        lines.append(
            "These cases are already in the bank - choose a materially different "
            "pathology:\n  " + "\n  ".join(f"- {t}" for t in avoid[:30])
        )
    lines.append(_style_examples(db, subspecialty))

    data = client.complete_json(
        task="generation", system=SYSTEM_PROMPT, user="\n".join(lines),
        max_tokens=16000, job_id=job_id,
    )
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Station generation did not return a JSON object")

    title = _clean(data.get("title"))
    if not title:
        return None
    if _is_duplicate(db, title, _clean(data.get("diagnosis"))):
        logger.info("Skipping near-duplicate station: %s", title)
        return None

    prompts, warnings = normalise_prompts(data.get("prompts") or [])
    if not prompts:
        raise ValueError("Station had no usable examiner prompts")

    # The flat rubric is kept alongside the per-prompt split so the station
    # matches the shape of the ingested ones.
    flat_rubric = [pt for p in prompts for pt in p["rubric"]]

    given = _clean(data.get("findings_given"))
    elicited = _clean(data.get("findings_elicited"))

    station = OsceStation(
        station_number=None,
        subspecialty=normalise_subspecialty(data.get("subspecialty")) or subspecialty,
        title=title,
        case_summary=_clean(data.get("case_summary")),
        aims=_string_list(data.get("aims")) or None,
        patient_history=_clean(data.get("patient_history")),
        patient_demographic=_clean(data.get("patient_demographic")),
        findings="\n".join(x for x in [given, elicited] if x) or None,
        findings_given=given,
        findings_elicited=elicited,
        # Generated stations arrive already split, so no separate pass is needed.
        findings_split_status="complete",
        diagnosis=_clean(data.get("diagnosis")),
        common_mistakes=_string_list(data.get("common_mistakes")) or None,
        tasks=[{"prompt": p["text"], "minutes": None} for p in prompts],
        rubric=flat_rubric,
        prompts=prompts,
        prompts_status="complete",
        total_marks=20,
        source=SOURCE_GENERATED,
        status=STATUS_REVIEW,
        angoff_expected=_angoff(data.get("angoff_expected")),
    )
    db.add(station)
    db.commit()
    db.refresh(station)

    if warnings:
        logger.info("Station %s: %s", station.id, "; ".join(warnings))
    return station.id


# Words too generic to distinguish one station from another.
_STOPWORDS = {
    "the", "and", "with", "from", "syndrome", "right", "left", "eye", "eyes",
    "of", "a", "an", "post", "following", "secondary", "chronic", "acute",
    "bilateral", "unilateral", "disease", "total",
}


def _signature(text: str | None) -> set[str]:
    return {
        w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
        if w not in _STOPWORDS
    }


def _is_duplicate(db: Session, title: str, diagnosis: str | None) -> bool:
    """Reject a station that repeats one already in the bank.

    Exact title matching is not enough - "Total Limbal Stem Cell Deficiency
    Following Chemical Injury" and "Limbal Stem Cell Deficiency following
    Chemical Injury, Right Eye" are the same case to a candidate. Overlap of
    the distinctive words catches those.
    """
    new = _signature(title) | _signature(diagnosis)
    if not new:
        return False

    for existing_title, existing_diagnosis in db.execute(
        select(OsceStation.title, OsceStation.diagnosis)
    ).all():
        old = _signature(existing_title) | _signature(existing_diagnosis)
        if not old:
            continue
        if len(new & old) / len(new | old) > 0.55:
            return True
    return False


def thin_subspecialties(db: Session, target: int) -> dict[str, int]:
    """How many more stations each subspecialty needs to reach `target`."""
    counts = dict(
        db.execute(
            select(OsceStation.subspecialty, func.count(OsceStation.id))
            .where(OsceStation.prompts_status == "complete")
            .group_by(OsceStation.subspecialty)
        ).all()
    )
    return {
        name: target - counts.get(name, 0)
        for name in SUBSPECIALTIES
        if counts.get(name, 0) < target
    }


@register_handler(JOB_GENERATE_STATIONS)
def handle_generate_stations(ctx: JobContext) -> bool:
    """One station per chunk - they are large, and a failure should cost one."""
    plan: list[str] = ctx.cursor_get("plan")
    if plan is None:
        requested: dict[str, int] = ctx.payload.get("per_subspecialty") or {}
        if not requested:
            raise JobHandlerError("Nothing requested")
        plan = [name for name, count in requested.items() for _ in range(int(count))]
        ctx.cursor_set(plan=plan)
        ctx.set_total(len(plan))

    index = ctx.cursor_get("index", 0)
    if index >= len(plan):
        return True

    subspecialty = plan[index]

    # Advance the cursor and commit BEFORE generating, not after. The job
    # runner is at-least-once: if the database connection drops between the
    # station being saved and the cursor moving on, the step re-runs and the
    # bank gains a duplicate. Claiming the step up front makes it at-most-once
    # instead, so a dropped connection costs one station rather than creating a
    # near-identical twin - and generating another is trivial.
    ctx.cursor_set(index=index + 1)
    ctx.db.commit()

    try:
        station_id = generate_station(
            ctx.db, AIClient(ctx.db), subspecialty,
            difficulty=ctx.payload.get("difficulty"), job_id=ctx.job.id,
        )
        key = "created" if station_id else "skipped"
        done = list((ctx.job.result or {}).get(key, []))
        done.append(station_id or subspecialty)
        ctx.set_result(**{key: done})
    except Exception as exc:  # noqa: BLE001 - one station must not stop the batch
        ctx.db.rollback()
        logger.exception("Station generation failed for %s", subspecialty)
        log_error(ctx.db, source="osce_generation", message=str(exc),
                  context={"subspecialty": subspecialty})
        failed = list((ctx.job.result or {}).get("failed", []))
        failed.append(subspecialty)
        ctx.set_result(failed=failed)

    ctx.advance(1, f"Stations: {index + 1} of {len(plan)}")

    finished = index + 1 >= len(plan)
    if finished:
        _queue_image_sourcing(ctx)
    return finished


def _queue_image_sourcing(ctx: JobContext) -> None:
    """A generated station arrives with no image at all.

    It comes out complete in every other way - findings already split, questions
    already in the examiner arc - so this is the only link missing, and without
    it a freshly generated station asks the candidate to examine something it
    cannot show them. Queued once the whole batch is done rather than per
    station, so nine stations cost one job.
    """
    from app.services.osce.station_images import JOB_SOURCE_STATION_IMAGES

    created = [i for i in (ctx.job.result or {}).get("created", []) if isinstance(i, int)]
    if not created:
        return
    job = create_job(
        ctx.db,
        JOB_SOURCE_STATION_IMAGES,
        payload={"station_ids": sorted(created), "only_missing": True},
        created_by_id=ctx.job.created_by_id,
        total_steps=len(created),
        message=f"Sourcing images for {len(created)} new station(s)",
    )
    logger.info("Queued image sourcing job %s for %d generated station(s)", job.id, len(created))


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


def _angoff(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None
