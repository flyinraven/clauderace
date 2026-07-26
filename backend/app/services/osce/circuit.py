"""OSCE station sittings: clock, circuit assembly and rubric marking."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    DEFAULT_ANGOFF_EXPECTED,
    EXAMINER_DISCREPANCY_THRESHOLD,
    OSCE_STATION_MINUTES,
    SUBSPECIALTIES,
)
from app.models import (
    OsceCircuit,
    OsceGrade,
    OsceResponse,
    OsceResult,
    OsceSession,
    OsceStation,
)
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_GRADE_OSCE = "grade_osce_session"

STATION_SECONDS = OSCE_STATION_MINUTES * 60
# Absorbs the upload of the final answer when the clock runs out mid-recording.
OSCE_GRACE_SECONDS = 20

EXAMINER_TEMPERATURES = {1: 0.0, 2: 0.35}


# --- Clock ----------------------------------------------------------------
@dataclass(frozen=True)
class StationClock:
    phase: str
    seconds_remaining: int
    seconds_elapsed: int
    ends_at: datetime | None
    can_record: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "seconds_remaining": self.seconds_remaining,
            "seconds_elapsed": self.seconds_elapsed,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "can_record": self.can_record,
            "station_seconds": STATION_SECONDS,
        }


def compute_station_clock(
    started_at: datetime | None,
    submitted_at: datetime | None = None,
    is_timed: bool = True,
    now: datetime | None = None,
) -> StationClock:
    """Nine minutes, derived from the start timestamp and nothing else."""
    now = now or datetime.now(timezone.utc)

    if submitted_at is not None:
        return StationClock("submitted", 0, STATION_SECONDS, None, False)
    if started_at is None:
        return StationClock("not_started", STATION_SECONDS, 0, None, False)
    if not is_timed:
        return StationClock("running", STATION_SECONDS, 0, None, True)

    ends_at = started_at + timedelta(seconds=STATION_SECONDS)
    elapsed = int((now - started_at).total_seconds())
    remaining = int((ends_at - now).total_seconds())

    if remaining > 0:
        return StationClock("running", remaining, elapsed, ends_at, True)
    # A recording already under way when time expires is still accepted.
    within_grace = remaining > -OSCE_GRACE_SECONDS
    return StationClock("expired", 0, elapsed, ends_at, within_grace)


# --- Circuit assembly -----------------------------------------------------
def build_circuit(
    db: Session, user_id: int, station_count: int = 9, scheduled_for: date | None = None
) -> OsceCircuit:
    """Pick one station per subspecialty from those this candidate has not sat.

    Repeating a station the candidate already knows the answer to teaches
    recall of that case rather than clinical reasoning, so an attempted station
    is never drawn again. Clearing the attempt puts it back in the pool - that
    is the deliberate "let me sit this one again", and it is per candidate:
    attempts are counted for this user only.

    A short circuit is better than a padded one. If there are not enough unseen
    stations left, the caller gets what there is and can say so.
    """
    attempted = set(
        db.execute(
            select(OsceSession.station_id).where(OsceSession.user_id == user_id)
        ).scalars().all()
    )

    ready = [
        s
        for s in db.execute(
            select(OsceStation).where(OsceStation.prompts_status == "complete")
        ).scalars().all()
        if s.id not in attempted
    ]
    if not ready:
        raise ValueError(
            "No unsat stations are left. Clear an attempt from the OSCE page to "
            "sit a station again, or ingest another OSCE report."
        )

    by_subspecialty: dict[str, list[OsceStation]] = defaultdict(list)
    for station in ready:
        by_subspecialty[station.subspecialty or "Unclassified"].append(station)

    chosen: list[OsceStation] = []
    for name in SUBSPECIALTIES:
        pool = by_subspecialty.get(name) or []
        if not pool:
            continue
        # Random rather than lowest id: taking the lowest handed every
        # candidate the same circuit and worked through the bank in ingestion
        # order, so the newest stations were always sat last.
        chosen.append(random.choice(pool))
        if len(chosen) >= station_count:
            break

    # Top up from anywhere if some subspecialties have no unsat station left.
    if len(chosen) < station_count:
        picked = {s.id for s in chosen}
        spare = [s for s in ready if s.id not in picked]
        random.shuffle(spare)
        for station in spare:
            chosen.append(station)
            picked.add(station.id)
            if len(chosen) >= station_count:
                break

    circuit = OsceCircuit(
        user_id=user_id,
        title=f"OSCE circuit — {(scheduled_for or date.today()).isoformat()}",
        scheduled_for=scheduled_for or date.today(),
        station_ids=[s.id for s in chosen],
        status="pending",
    )
    db.add(circuit)
    db.commit()
    db.refresh(circuit)
    return circuit


# --- Marking --------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a RANZCO examiner marking one question at a station of the RACE OSCE. \
You are given the question you asked, the marking rubric for that question, and \
a transcript of what the candidate said aloud in reply.

The first question of a station is not a question but the standing instruction \
- "Please examine the anterior segment of the left eye". What comes back is \
the candidate's running description of what they see, in whatever order they \
noticed it. Mark that description against the rubric exactly as you would any \
other answer: credit each sign they named, and do not expect them to have \
answered a question that was never asked.

Mark exactly as an examiner would:
- Award marks rubric point by rubric point. A point is earned if the candidate \
conveyed that idea aloud, in any reasonable wording.
- This is SPOKEN language transcribed automatically. Ignore disfluencies, false \
starts, repetition and grammar. Expect transcription errors in eponyms and drug \
names - if a word is clearly a mangled version of the right term, credit it.
- Candidates speak in note form under time pressure. Do not require prose.
- Do NOT award marks for content that is not in the rubric. Do NOT deduct marks \
for extra correct material.
- If the candidate says something clinically dangerous or plainly wrong, award \
nothing for that rubric point and say so in the comment.
- [inaudible] markers mean the recording failed there, not that the candidate \
was silent. Do not penalise them specifically, but you can only mark what you \
can read.

Return ONLY a JSON object:
{
  "breakdown": [
    {"index": <integer, the rubric point's 0-based index as given>,
     "awarded": <number>,
     "comment": "<one short sentence on why>"}
  ],
  "awarded_total": <number>,
  "feedback": "<two or three sentences of examiner feedback: what was said well
                and specifically what was missing>"
}"""


def _prompt_for(station: OsceStation, prompt: dict[str, Any], transcript: str) -> str:
    rubric_lines = "\n".join(
        f"  index={i} | {pt.get('marks', 0):g} mark(s) | {pt.get('text', '')}"
        + ("  [CRITICAL - examiners specifically looked for this]" if pt.get("is_critical") else "")
        for i, pt in enumerate(prompt.get("rubric") or [])
    )
    return (
        f"STATION: {station.title or station.subspecialty or 'OSCE station'}\n"
        f"SUBSPECIALTY: {station.subspecialty or 'unspecified'}\n\n"
        f"CASE:\n{station.case_summary or '(none)'}\n\n"
        f"PATIENT HISTORY:\n{station.patient_history or '(none)'}\n\n"
        f"EXAMINATION FINDINGS SHOWN TO THE CANDIDATE:\n{station.findings or '(none)'}\n\n"
        f"THE QUESTION YOU ASKED:\n{prompt.get('text', '')}\n\n"
        f"RUBRIC FOR THIS QUESTION:\n{rubric_lines or '  (none)'}\n\n"
        f"TRANSCRIPT OF THE CANDIDATE'S SPOKEN ANSWER:\n"
        f'"""\n{transcript.strip() or "(the candidate said nothing)"}\n"""\n\n'
        f"Mark it now. The maximum you may award is "
        f"{sum(pt.get('marks', 0) for pt in (prompt.get('rubric') or [])):g}."
    )


def grade_prompt(
    db: Session,
    client: AIClient,
    session: OsceSession,
    station: OsceStation,
    prompt: dict[str, Any],
    transcript: str,
    examiner_pass: int,
    job_id: int | None = None,
) -> OsceGrade:
    rubric = prompt.get("rubric") or []
    available = float(sum(pt.get("marks", 0) for pt in rubric))
    label = prompt.get("label") or "?"

    grade = _upsert_grade(db, session.id, label, examiner_pass)
    grade.available_marks = available

    if not rubric:
        # Nothing to mark against, so there is nothing a model can add: it
        # would be asked to score an answer against an empty standard.
        grade.awarded_marks = 0.0
        grade.breakdown = []
        grade.feedback = "This question carries no marks."
        db.commit()
        return grade

    if not transcript.strip():
        grade.awarded_marks = 0.0
        grade.breakdown = [
            {
                "index": i,
                "text": pt.get("text"),
                "marks": pt.get("marks", 0),
                "awarded": 0.0,
                "comment": "No answer was recorded.",
                "is_critical": bool(pt.get("is_critical")),
            }
            for i, pt in enumerate(rubric)
        ]
        grade.feedback = "Nothing was recorded for this question."
        grade.model_used = "n/a"
        db.commit()
        return grade

    data = client.complete_json(
        task="grading",
        system=SYSTEM_PROMPT,
        user=_prompt_for(station, prompt, transcript),
        temperature=EXAMINER_TEMPERATURES.get(examiner_pass, 0.2),
        job_id=job_id,
    )
    if not isinstance(data, dict):
        raise ValueError("OSCE grading response was not a JSON object")

    breakdown: list[dict[str, Any]] = []
    total = 0.0
    for item in data.get("breakdown") or []:
        if not isinstance(item, dict):
            continue
        index = _as_int(item.get("index"))
        if index is None or not (0 <= index < len(rubric)):
            continue
        point = rubric[index]
        point_marks = float(point.get("marks", 0))
        awarded = max(0.0, min(_as_float(item.get("awarded"), 0.0), point_marks))
        total += awarded
        breakdown.append(
            {
                "index": index,
                "text": point.get("text"),
                "marks": point_marks,
                "awarded": round(awarded, 2),
                "comment": str(item.get("comment") or "").strip() or None,
                "is_critical": bool(point.get("is_critical")),
            }
        )

    grade.awarded_marks = round(min(total, available), 2)
    grade.breakdown = breakdown
    grade.feedback = str(data.get("feedback") or "").strip() or None
    grade.model_used = client.model_for("grading")
    db.commit()
    return grade


def _examiner_passes(db: Session) -> tuple[int, ...]:
    """Shared with the written papers - see app.services.grading.grade."""
    from app.services.grading.grade import _examiner_passes as passes

    return passes(db)


def _upsert_grade(db: Session, session_id: int, label: str, examiner_pass: int) -> OsceGrade:
    existing = db.execute(
        select(OsceGrade)
        .where(OsceGrade.session_id == session_id)
        .where(OsceGrade.prompt_label == label)
        .where(OsceGrade.examiner_pass == examiner_pass)
    ).scalar_one_or_none()
    if existing:
        return existing
    grade = OsceGrade(session_id=session_id, prompt_label=label, examiner_pass=examiner_pass)
    db.add(grade)
    db.flush()
    return grade


@register_handler(JOB_GRADE_OSCE)
def handle_grade_osce_session(ctx: JobContext) -> bool:
    """Mark one prompt per chunk, both examiner passes together."""
    session_id = ctx.payload.get("session_id")
    if not session_id:
        raise JobHandlerError("Grading job is missing session_id")

    session = ctx.db.get(OsceSession, session_id)
    if session is None:
        raise JobHandlerError(f"OSCE sitting {session_id} no longer exists")
    station = ctx.db.get(OsceStation, session.station_id)
    if station is None or not station.prompts:
        raise JobHandlerError("This station has no examiner prompts to mark against")

    prompts = station.prompts
    if not ctx.job.total_steps:
        ctx.set_total(len(prompts))
        session.grading_status = "running"
        ctx.db.commit()

    index = ctx.cursor_get("index", 0)
    if index >= len(prompts):
        summarise_osce_session(ctx.db, session)
        return True

    prompt = prompts[index]
    label = prompt.get("label") or str(index)
    response = ctx.db.execute(
        select(OsceResponse)
        .where(OsceResponse.session_id == session.id)
        .where(OsceResponse.prompt_label == label)
    ).scalar_one_or_none()
    transcript = response.marking_text if response else ""

    client = AIClient(ctx.db)
    for examiner_pass in _examiner_passes(ctx.db):
        try:
            grade_prompt(
                ctx.db, client, session, station, prompt, transcript, examiner_pass,
                job_id=ctx.job.id,
            )
        except Exception as exc:  # noqa: BLE001 - one prompt must not stop the station
            ctx.db.rollback()
            logger.exception("OSCE grading failed for %s pass %s", label, examiner_pass)
            log_error(
                ctx.db, source="osce_grading", message=f"{label} pass {examiner_pass}: {exc}",
                context={"session_id": session.id, "prompt": label},
            )
            failed = list((ctx.job.result or {}).get("failed_prompts", []))
            failed.append(label)
            ctx.set_result(failed_prompts=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Marked {index + 1} of {len(prompts)} questions")

    if index + 1 >= len(prompts):
        summarise_osce_session(ctx.db, session)
        return True
    return False


def summarise_osce_session(db: Session, session: OsceSession) -> OsceResult:
    """Average the two examiners and produce the station result."""
    station = db.get(OsceStation, session.station_id)
    prompts = (station.prompts if station else None) or []
    expected = {p.get("label") or str(i) for i, p in enumerate(prompts)}

    grades = db.execute(
        select(OsceGrade).where(OsceGrade.session_id == session.id)
    ).scalars().all()

    by_prompt: dict[str, list[OsceGrade]] = defaultdict(list)
    for grade in grades:
        by_prompt[grade.prompt_label].append(grade)

    ungraded = sorted(expected - set(by_prompt))

    total_awarded = 0.0
    total_available = 0.0
    flagged: list[str] = []

    for label, prompt_grades in by_prompt.items():
        available = max(g.available_marks for g in prompt_grades)
        awarded = sum(g.awarded_marks for g in prompt_grades) / len(prompt_grades)
        if len(prompt_grades) > 1 and available > 0:
            spread = max(g.awarded_marks for g in prompt_grades) - min(
                g.awarded_marks for g in prompt_grades
            )
            if spread / available > EXAMINER_DISCREPANCY_THRESHOLD:
                flagged.append(label)
        total_awarded += awarded
        total_available += available

    percentage = (total_awarded / total_available * 100) if total_available else 0.0

    expectation = (station.angoff_expected if station else None) or DEFAULT_ANGOFF_EXPECTED
    cut_score = round(total_available * float(expectation), 2) if total_available else None

    # As with the written papers, a partly-marked station gets no verdict.
    outcome = None
    if ungraded:
        outcome = "incomplete"
    elif cut_score is not None:
        outcome = "pass" if total_awarded >= cut_score else "fail"

    result = db.execute(
        select(OsceResult).where(OsceResult.session_id == session.id)
    ).scalar_one_or_none()
    if result is None:
        result = OsceResult(session_id=session.id)
        db.add(result)

    result.total_awarded = round(total_awarded, 2)
    result.total_available = round(total_available, 2)
    result.percentage = round(percentage, 1)
    result.cut_score = cut_score
    result.outcome = outcome
    result.flagged_prompts = flagged
    result.ungraded_prompts = ungraded
    result.overall_feedback = _station_feedback(
        total_awarded, total_available, percentage, cut_score, ungraded, len(expected)
    )

    session.grading_status = "partial" if ungraded else "complete"
    db.commit()
    return result


def _station_feedback(
    awarded: float,
    available: float,
    percentage: float,
    cut_score: float | None,
    ungraded: list[str],
    expected: int,
) -> str:
    if ungraded:
        return (
            f"This station could not be fully marked: {len(ungraded)} of {expected} "
            f"questions failed to mark. The score shown covers only what was marked, "
            f"so no pass/fail verdict is given. Use 'Re-mark' to complete it."
        )
    lines = [f"You scored {awarded:.1f} of {available:.0f} marks ({percentage:.1f}%)."]
    if cut_score is not None:
        margin = awarded - cut_score
        lines.append(
            f"The pass standard for this station is {cut_score:.1f} marks, so you are "
            f"{abs(margin):.1f} {'above' if margin >= 0 else 'below'} it."
        )
    return " ".join(lines)


def circuit_progress(db: Session, circuit: OsceCircuit) -> dict[str, Any]:
    sittings = db.execute(
        select(OsceSession).where(OsceSession.circuit_id == circuit.id)
    ).scalars().all()
    done = [s for s in sittings if s.submitted_at is not None]
    total_awarded = 0.0
    total_available = 0.0
    for sitting in done:
        result = db.execute(
            select(OsceResult).where(OsceResult.session_id == sitting.id)
        ).scalar_one_or_none()
        if result:
            total_awarded += result.total_awarded
            total_available += result.total_available
    return {
        "stations": len(circuit.station_ids or []),
        "completed": len(done),
        "total_awarded": round(total_awarded, 2),
        "total_available": round(total_available, 2),
        "percentage": round(total_awarded / total_available * 100, 1) if total_available else None,
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "JOB_GRADE_OSCE",
    "STATION_SECONDS",
    "StationClock",
    "build_circuit",
    "circuit_progress",
    "compute_station_clock",
    "grade_prompt",
    "handle_grade_osce_session",
    "summarise_osce_session",
]
