"""Turn the flat OSCE station record into a timed examiner conversation.

The examiners' reports give each station as a case summary, findings, diagnosis
and a 20-mark rubric - but a real OSCE is a dialogue: the examiner asks, the
candidate speaks, the examiner asks the next thing. This converts a station
into that ordered sequence, splitting the rubric so every spoken answer is
marked against exactly what was asked of it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import OSCE_STATION_MINUTES
from app.models import OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_BUILD_OSCE_PROMPTS = "build_osce_prompts"

STATION_SECONDS = OSCE_STATION_MINUTES * 60  # 540
MIN_PROMPTS = 3
MAX_PROMPTS = 7

SYSTEM_PROMPT = f"""\
You are a RANZCO examiner running one station of the RACE OSCE. A station lasts \
exactly {OSCE_STATION_MINUTES} minutes ({STATION_SECONDS} seconds) and is a \
spoken dialogue: you ask a question, the candidate answers aloud, then you ask \
the next one.

You are given a station's case, findings, diagnosis and its 20-mark rubric. \
Convert it into the ordered sequence of questions you would actually ask.

Rules:
- The FIRST question is always the standing instruction the candidate is given \
as they walk in, naming the region to examine and the eye: "Please examine the \
anterior segment + posterior pole of the left eye", "Please examine right \
anterior segments and posterior pole", "Please perform a strabismus \
examination", "Please examine the orbit". Nothing else - no history, no \
findings, no hint of the diagnosis. That is how a real station opens.
- After that, questions are what an examiner actually asks at the slit lamp. \
Terse, spoken, second person. Mix these kinds, roughly in this order:
  * the findings and the diagnosis: "What is the likely diagnosis and \
differentials?"
  * classification or list questions that leave this patient behind: "What are \
the types of paediatric glaucoma?", "What are the differentials in a child \
with epiphora and photophobia?"
  * a hypothetical variation on the case: "What if they had an opaque cornea?"
  * management, asked as a plan: "Pt has a high IOP as a child - what is your \
immediate management plan?"
  * a question that reaches back into the history: "This patient has had \
multiple corneal grafts - what could be reasons why this was necessary?"
  * where it fits, ask for a stated number: "Name 2 systemic associations or \
genes involved in ocular coloboma"
- Do not number the questions in their text, and do not preface them with \
"Question 3" or "Next". Say only what the examiner would say.

A real station, as written by the examiner who ran it - match this register:
  "Please perform anterior segment examination for both eyes and perform \
retinoscopy for the right eye only. His right pupil has been dilated."
  "What is the presumed diagnosis?"
  "How would you confirm the diagnosis?"
  "What would be your general management of this patient if he was new to \
your practice?"
  "What are the criteria for keratoconus progression?"
  "What are the risk factors for developing keratoconus? Name 4."
  "If spectacle corrected vision is unsatisfactory and he is intolerant of \
RGP, what are the other management options?"
- Produce between {MIN_PROMPTS} and {MAX_PROMPTS} questions, in the order a real \
examiner would ask them: examine and describe findings first, then \
interpretation and diagnosis, then differential, then investigation and \
management, then any complication or counselling question.
- Word each question exactly as you would say it aloud to the candidate. Short \
and direct. "Please examine the anterior segment and describe your findings." \
not "The candidate should be asked to...".
- Give each question a time in seconds. The times MUST total exactly \
{STATION_SECONDS}. Weight them by how much the question is worth.
- Split the supplied 20-mark rubric across the questions. Every rubric point \
must appear under exactly one question, reworded only if needed to read as a \
markable expectation. The marks across ALL questions must total exactly 20.
- Where the examiners noted a common mistake, make sure the question that would \
expose it is present, and mark that rubric point is_critical.
- Keep the opening instruction exactly as an examiner gives it - "Please \
examine..." - even though there is no live patient here: the candidate is \
shown the station's photograph and answers from it. Later questions that would \
need a hands-on manoeuvre should ask what they would look for and what it \
would show, rather than pretending the examination happened.

Return ONLY a JSON object:
{{
  "prompts": [
    {{"label": "A",
      "text": "the question as spoken",
      "seconds": <integer>,
      "rubric": [
        {{"text": "markable expectation", "marks": <number>,
          "is_critical": <boolean>}}
      ]}}
  ]
}}"""


def build_prompts_for_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Generate and persist the examiner prompt sequence for one station."""
    rubric_lines = "\n".join(
        f"  - {item.get('marks', 0):g} mark(s): {item.get('text', '')}"
        for item in (station.rubric or [])
    ) or "  (no rubric recorded)"

    mistakes = "\n".join(f"  - {m}" for m in (station.common_mistakes or [])) or "  (none recorded)"

    user = (
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n"
        f"STATION: {station.title or f'Station {station.station_number}'}\n\n"
        f"CASE SUMMARY:\n{station.case_summary or '(none)'}\n\n"
        f"AIMS OF THE STATION:\n"
        + ("\n".join(f"  - {a}" for a in (station.aims or [])) or "  (none)")
        + f"\n\nPATIENT HISTORY:\n{station.patient_history or '(none)'}\n\n"
        f"EXAMINATION FINDINGS:\n{station.findings or '(none)'}\n\n"
        f"DIAGNOSIS:\n{station.diagnosis or '(none)'}\n\n"
        f"MARKING RUBRIC (20 marks total):\n{rubric_lines}\n\n"
        f"MISTAKES THE EXAMINERS NOTED IN THE REAL COHORT:\n{mistakes}\n\n"
        f"Write the examiner's question sequence now. Check your arithmetic: "
        f"seconds must total {STATION_SECONDS} and marks must total 20."
    )

    data = client.complete_json(
        task="utility", system=SYSTEM_PROMPT, user=user, job_id=job_id
    )
    if isinstance(data, list):
        data = {"prompts": data}
    if not isinstance(data, dict):
        raise ValueError("Prompt generation did not return a JSON object")

    prompts, warnings = _normalise(data.get("prompts") or [])
    if not prompts:
        raise ValueError("No usable prompts were produced")

    station.prompts = prompts
    station.prompts_status = "complete"
    meta_warnings = warnings
    db.commit()
    return {"prompts": len(prompts), "warnings": meta_warnings}


def _normalise(raw_prompts: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce to the stored shape and force the timing and marks to add up."""
    warnings: list[str] = []
    prompts: list[dict[str, Any]] = []

    for index, item in enumerate(raw_prompts):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue

        rubric: list[dict[str, Any]] = []
        for point in item.get("rubric") or []:
            if not isinstance(point, dict) or not point.get("text"):
                continue
            rubric.append(
                {
                    "text": str(point["text"]).strip(),
                    "marks": _as_float(point.get("marks"), 0.0),
                    "is_critical": bool(point.get("is_critical")),
                }
            )

        prompts.append(
            {
                "label": str(item.get("label") or chr(ord("A") + index)).strip(),
                "text": text,
                "seconds": max(15, int(_as_float(item.get("seconds"), 0.0) or 0)),
                "rubric": rubric,
            }
        )

    if not prompts:
        return [], warnings

    # Timing must fill the station exactly; the candidate is entitled to the
    # full nine minutes and not a second more.
    total_seconds = sum(p["seconds"] for p in prompts)
    if total_seconds != STATION_SECONDS:
        warnings.append(f"Prompt times totalled {total_seconds}s; rescaled to {STATION_SECONDS}s.")
        factor = STATION_SECONDS / total_seconds
        for prompt in prompts:
            prompt["seconds"] = max(15, int(round(prompt["seconds"] * factor)))
        drift = STATION_SECONDS - sum(p["seconds"] for p in prompts)
        prompts[-1]["seconds"] = max(15, prompts[-1]["seconds"] + drift)

    # Marks must total 20, for the same reason they must in a written paper.
    total_marks = sum(pt["marks"] for p in prompts for pt in p["rubric"])
    if total_marks > 0 and abs(total_marks - 20) > 0.01:
        warnings.append(f"Rubric totalled {total_marks:g} marks; rescaled to 20.")
        factor = 20 / total_marks
        for prompt in prompts:
            for point in prompt["rubric"]:
                point["marks"] = round(point["marks"] * factor, 2)
        _absorb_mark_drift(prompts)
    elif total_marks == 0:
        warnings.append("No rubric marks were produced for this station.")

    return prompts, warnings


def _absorb_mark_drift(prompts: list[dict[str, Any]]) -> None:
    """Put the rounding remainder on the largest rubric point."""
    points = [pt for p in prompts for pt in p["rubric"]]
    if not points:
        return
    drift = round(20 - sum(pt["marks"] for pt in points), 2)
    if abs(drift) < 0.005:
        return
    target = max(points, key=lambda pt: pt["marks"])
    target["marks"] = round(max(0.0, target["marks"] + drift), 2)


# --- Job handler ----------------------------------------------------------
@register_handler(JOB_BUILD_OSCE_PROMPTS)
def handle_build_osce_prompts(ctx: JobContext) -> bool:
    """Build prompt sequences, one station per chunk."""
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
            outcome = build_prompts_for_station(
                ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id
            )
            done = list((ctx.job.result or {}).get("completed", []))
            done.append(station.id)
            ctx.set_result(completed=done)
            if outcome["warnings"]:
                warned = list((ctx.job.result or {}).get("warnings", []))
                warned.extend(f"Station {station.station_number}: {w}" for w in outcome["warnings"])
                ctx.set_result(warnings=warned)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the batch
            ctx.db.rollback()
            logger.exception("Prompt build failed for station %s", station.id)
            log_error(
                ctx.db, source="osce_prompts", message=str(exc),
                context={"station_id": station.id},
            )
            station.prompts_status = "failed"
            ctx.db.commit()
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(station.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Stations prepared: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


def stations_needing_prompts(db: Session) -> list[int]:
    return list(
        db.execute(
            select(OsceStation.id)
            .where(OsceStation.prompts_status.in_(["none", "failed"]))
            .order_by(OsceStation.id)
        ).scalars().all()
    )


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
