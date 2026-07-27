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

from app.constants import OSCE_STATION_MARKS, OSCE_STATION_MINUTES
from app.models import OsceStation
from app.services.ai import AIClient
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.marking import absorb_mark_drift

logger = logging.getLogger(__name__)

JOB_BUILD_OSCE_PROMPTS = "build_osce_prompts"

STATION_SECONDS = OSCE_STATION_MINUTES * 60  # 540
STATION_MARKS = OSCE_STATION_MARKS
MIN_PROMPTS = 3
# The arc below runs to seven steps: instruction, ancillary test, read the
# image, differentials, the diagnosis-and-management question, an evolving
# hypothetical and a knowledge question. One question per step, no more.
MAX_PROMPTS = 7

SYSTEM_PROMPT = f"""\
You are a RANZCO examiner running one station of the RACE OSCE. A station lasts \
exactly {OSCE_STATION_MINUTES} minutes ({STATION_SECONDS} seconds) and is a \
spoken dialogue: you ask a question, the candidate answers aloud, then you ask \
the next one.

You are given a station's case, findings, diagnosis and its 20-mark rubric. \
Convert it into the ordered sequence of questions you would actually ask.

THE ONE RULE ABOVE ALL OTHERS: a question must never contain its own answer.
The candidate is being tested on whether they can find the signs and name the
disease. So until the examiner formally gives the diagnosis at step 5, NO
question may name a finding, a sign, an imaging appearance, the diagnosis, or
the region of pathology. Write "Now describe the fundus" - never "Now describe
the dragged macula", "in this post-vitrectomy eye", "for this type of
choroiditis", or "including the macula and periphery", which all tell the
candidate where to look or what they are going to find.

How a RANZCO station is actually built, from real examiner handouts. Follow
this arc, in this order. Steps 1, 2, 4 and 5 are REQUIRED in every station;
step 3 is required whenever the station has an image; and you must include at
least one of steps 6 and 7:

1. THE STANDING INSTRUCTION. The first question is always what the candidate
   is told as they walk in: the region and the eye, nothing else. "Please
   examine the posterior segment of both eyes." "Please examine the anterior
   segment of the left eye." "Please perform anterior segment examination for
   both eyes and perform retinoscopy for the right eye only." No history, no
   findings, no hint of the diagnosis, no list of structures to check, and no
   "and describe what you see" shopping list.
   It is still marked, and heavily: what comes back is the candidate's
   description of the signs, so EVERY rubric point about identifying or
   describing a finding belongs here. It must never carry zero marks.
2. WHAT ELSE WOULD YOU DO. "What other examinations would you do in this
   patient?" / "What ancillary test would you perform?" The candidate should
   name the test before being shown it - so do NOT name it yourself.
3. READ THE IMAGE. Having asked for it, they describe what it shows -
   correctly naming the sign, its extent, and what is absent. Ask it blind:
   "What does this show?" / "Describe the OCT."
4. SUMMARISE AND DIFFERENTIATE, with a stated number: "Can you summarise your
   findings and give 5 differential diagnoses?"
5. THE EXAMINER GIVES THE DIAGNOSIS AND ASKS FOR MANAGEMENT. This is ONE
   question, and the pair is the point of it: state the diagnosis plainly -
   "The presumed diagnosis is amelanotic iris melanoma" - and in the same
   breath ask for the plan, framed as ownership: "How would you manage him if
   he were new to you and you had just made the diagnosis?" Giving it away
   here is deliberate: a station must keep going even when the candidate has
   not got there, and later marks must not depend on earlier ones. This is the
   FIRST question in which the diagnosis may be spoken, and from here on
   naming it is expected.
6. THE CASE MOVES ON - a hypothetical that evolves it in time or severity:
   "You observe the patient for 5 years, there has been minimal change. He
   develops a cataract and vision drops to 6/18. What are your options?" /
   "If a ciliary body lesion were found on UBM, what further investigations
   would you do?" / "What if they had an opaque cornea?"
7. STRAIGHT KNOWLEDGE, off this patient entirely: criteria, inheritance,
   classification, risk factors - and ask for a number where one exists.
   "What are the criteria for keratoconus progression?" "What is the
   inheritance pattern?" "What are the risk factors for developing
   keratoconus? Name 4." "What are the types of paediatric glaucoma?"

A worked example of the whole arc, for a station whose diagnosis is
serpiginous choroiditis with a secondary CNVM. Note how nothing before E gives
anything away:

  A. (step 1) "Please examine the posterior segment of both eyes."
  B. (step 2) "What other investigations would you perform in this patient?"
  C. (step 3) "This is her OCT and fluorescein angiogram. What do they show?"
  D. (step 4) "Please summarise your findings and give me 4 differential
      diagnoses."
  E. (step 5) "The diagnosis is serpiginous choroiditis with a secondary
      choroidal neovascular membrane. How would you manage her if she were new
      to your practice today?"
  F. (step 6) "Her Mantoux and QuantiFERON Gold come back positive. What is
      the significance of that, and what changes?"
  G. (step 7) "What are the causes of a serpiginous-like choroiditis? Name 4."

Register, from the handouts - match it exactly:
- Short, spoken, second person, ONE thing asked at a time. Do not staple two
  questions together with "and" - "describe the findings and give your leading
  diagnosis" is two questions, and each belongs to its own step of the arc.
  Step 5 is the sole exception: there the diagnosis and the management ask are
  one question.
- "How would you confirm the diagnosis?" not "The candidate should be asked to
  confirm the diagnosis."
- Ask for a stated number wherever the answer is a list: "Name 4", "give 5
  differential diagnoses".
- Refer to the patient as a person - "How would you manage him if he were new
  to your practice?"
- Never number the questions in their text, and never preface them with
  "Question 3" or "Next". Say only what the examiner would say.

Other rules:
- Produce between {MIN_PROMPTS} and {MAX_PROMPTS} questions.
- Give each question the number of the arc step it came from, in "step". No
  step may appear twice, and they must be in ascending order.
- Keep the opening instruction as an examiner gives it - "Please examine..." -
  even though there is no live patient: the candidate is shown the station's
  photograph and answers from it. Later questions needing a hands-on manoeuvre
  should ask what they would look for and what it would show.
- Give each question a time in seconds. The times MUST total exactly \
{STATION_SECONDS}. Weight them by how much the question is worth.
- Split the supplied 20-mark rubric across the questions. Every rubric point \
must appear under exactly one question, reworded only if needed to read as a \
markable expectation. The marks across ALL questions must total exactly 20.
- Where the examiners noted a common mistake, make sure the question that would \
expose it is present, and mark that rubric point is_critical.

Return ONLY a JSON object:
{{
  "prompts": [
    {{"label": "A",
      "text": "the question as spoken",
      "step": <integer 1-7, the arc step this question is>,
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

    # A station with no photograph cannot ask the candidate to read one, so the
    # arc skips step 3 rather than inventing an image that is never shown.
    has_image = bool(station.figures)
    image_note = (
        "This station has an image, so arc step 3 is required."
        if has_image
        else "This station has NO image. Skip arc step 3 entirely - never ask the "
        "candidate to describe a photograph, OCT or angiogram they will not be shown."
    )

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
        f"{image_note}\n\n"
        f"Write the examiner's question sequence now. Check your arithmetic: "
        f"seconds must total {STATION_SECONDS} and marks must total 20."
    )

    prompts, warnings = _generate(client, user, job_id)

    # The arc is the whole point of the station, and the model does drop steps
    # or give the diagnosis away in the opening instruction. Say what is wrong
    # and ask once more rather than shipping a station that examines nothing.
    problems = _arc_problems(prompts, has_image)
    if problems:
        retry_user = (
            user
            + "\n\nYour first attempt was rejected because:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nRewrite the whole sequence, fixing these."
        )
        retried, retry_warnings = _generate(client, retry_user, job_id)
        remaining = _arc_problems(retried, has_image)
        # Keep whichever attempt is closer to a real station; a second try that
        # is still imperfect is usually still better than the first.
        if len(remaining) <= len(problems):
            prompts, warnings, problems = retried, retry_warnings, remaining
        if problems:
            warnings.append("Question arc is incomplete: " + "; ".join(problems))

    station.prompts = prompts
    station.prompts_status = "complete"
    meta_warnings = warnings
    db.commit()
    return {"prompts": len(prompts), "warnings": meta_warnings}


def _generate(
    client: AIClient, user: str, job_id: int | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """One round trip, normalised. Raises if nothing usable comes back."""
    data = client.complete_json(
        task="utility",
        system=SYSTEM_PROMPT,
        user=user,
        # Seven questions each carrying rubric points is a long reply, and the
        # utility model spends output tokens reasoning before it writes any of
        # it. At the 8k default some stations were cut off mid-JSON and lost.
        max_tokens=16000,
        job_id=job_id,
    )
    if isinstance(data, list):
        data = {"prompts": data}
    if not isinstance(data, dict):
        raise ValueError("Prompt generation did not return a JSON object")

    prompts, warnings = _normalise(data.get("prompts") or [])
    if not prompts:
        raise ValueError("No usable prompts were produced")
    return prompts, warnings


# Openings that have crept in and give the game away: they tell the candidate
# which structure is abnormal, or hand them a checklist to describe.
_OPENING_GIVEAWAYS = (
    "describe what you see",
    "describe the findings",
    "what you would look for",
    "including",
    "making sure",
    "paying attention",
)

_REQUIRED_STEPS = (1, 2, 4, 5)


def _arc_problems(prompts: list[dict[str, Any]], has_image: bool = True) -> list[str]:
    """Check the sequence against the arc. Empty means it is a real station."""
    problems: list[str] = []
    steps = [p.get("step") for p in prompts]

    # Step 3 is reading the image, which a station without one cannot ask.
    required = (*_REQUIRED_STEPS, 3) if has_image else _REQUIRED_STEPS
    for step in sorted(required):
        if steps.count(step) != 1:
            problems.append(
                f"arc step {step} appears {steps.count(step)} times; it must appear exactly once"
            )
    if not any(s in (6, 7) for s in steps):
        problems.append("neither a hypothetical (step 6) nor a knowledge question (step 7) is present")

    ordered = [s for s in steps if isinstance(s, int)]
    if ordered != sorted(ordered):
        problems.append("the questions are not in arc order")

    opening = prompts[0]
    if opening.get("step") != 1:
        problems.append("the first question is not the standing instruction")
    else:
        lowered = opening["text"].lower()
        for phrase in _OPENING_GIVEAWAYS:
            if phrase in lowered:
                problems.append(
                    f"the standing instruction says {phrase!r}; it must give the region and "
                    "the eye and nothing else"
                )
                break
        if not any(pt["marks"] > 0 for pt in opening["rubric"]):
            problems.append(
                "the standing instruction carries no marks; every rubric point about "
                "identifying or describing a finding belongs to it"
            )

    return problems


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
                    "marks": as_float(point.get("marks"), 0.0),
                    "is_critical": bool(point.get("is_critical")),
                }
            )

        prompts.append(
            {
                "label": str(item.get("label") or chr(ord("A") + index)).strip(),
                "text": text,
                # Which step of the examiner's arc this is; used to check the
                # sequence actually examines the candidate, and kept so a
                # station can be audited later.
                "step": int(as_float(item.get("step"), 0.0) or 0) or None,
                "seconds": max(15, int(as_float(item.get("seconds"), 0.0) or 0)),
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
    if total_marks > 0 and abs(total_marks - STATION_MARKS) > 0.01:
        warnings.append(
            f"Rubric totalled {total_marks:g} marks; rescaled to {STATION_MARKS}."
        )
        factor = STATION_MARKS / total_marks
        for prompt in prompts:
            for point in prompt["rubric"]:
                point["marks"] = round(point["marks"] * factor, 2)
        absorb_mark_drift([pt for p in prompts for pt in p["rubric"]], STATION_MARKS)
    elif total_marks == 0:
        warnings.append("No rubric marks were produced for this station.")

    return prompts, warnings


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

