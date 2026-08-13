"""Sitting a station: the clock, the spoken answers, and the result.

This is the only part a candidate touches, and the only part where what the
server sends back has to be watched - a station's case summary, its title and
its marking key must not reach the browser until the result does."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from app.api.deps import CurrentUser, DbSession
from app.models import (
    AudioClip,
    OsceCircuit,
    OsceGrade,
    OsceResponse,
    OsceResult,
    OsceSession,
    OsceStation,
)
from app.services.jobs.runner import create_job
from app.services.osce.coverage import sittable_prompts
from app.services.osce.circuit import JOB_GRADE_OSCE
from app.services.osce.transcribe_job import JOB_TRANSCRIBE_RESPONSE
from app.services.settings_store import SettingsStore
from app.api.osce.helpers import (
    ACCEPTED_AUDIO_PREFIXES,
    MAX_AUDIO_BYTES,
    _bound_figure_ids,
    figures_for_prompt,
    opening_figures_payload,
    _clock,
    _load_sitting,
)
from app.api.osce.circuits import _circuit_next

router = APIRouter()


# --- Sittings -------------------------------------------------------------
class StartSittingRequest(BaseModel):
    station_id: int
    circuit_id: int | None = None
    is_timed: bool = True


@router.post("/sittings", status_code=status.HTTP_201_CREATED)
def start_sitting(
    payload: StartSittingRequest, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    station = db.get(OsceStation, payload.station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")
    if not station.prompts:
        raise HTTPException(
            status_code=400,
            detail="This station has no examiner questions yet. An administrator "
                   "needs to prepare it first.",
        )
    if payload.circuit_id is not None:
        # The circuit id arrives from the client, and a sitting filed against
        # someone else's circuit counts towards their progress: their card would
        # read further along than they had sat. Nothing of theirs leaks either
        # way - the results query filters by user - but a candidate's own
        # progress must be their own work.
        circuit = db.get(OsceCircuit, payload.circuit_id)
        if circuit is None:
            raise HTTPException(status_code=404, detail="Circuit not found")
        if circuit.user_id != user.id:
            raise HTTPException(
                status_code=403, detail="That circuit belongs to someone else"
            )
    sitting = OsceSession(
        user_id=user.id,
        station_id=station.id,
        circuit_id=payload.circuit_id,
        is_timed=payload.is_timed,
    )
    db.add(sitting)
    db.commit()
    db.refresh(sitting)
    return {"id": sitting.id, "station_id": station.id}


@router.delete("/attempts", status_code=status.HTTP_200_OK)
def clear_all_attempts(user: CurrentUser, db: DbSession) -> dict[str, int]:
    """Forget every attempt this candidate has made, across all stations.

    Testing a station counts as sitting it, which then hides it from circuits.
    Rather than clearing a dozen stations one at a time after a test run, wipe
    the lot. Only this candidate's sittings go.
    """
    sittings = db.execute(
        select(OsceSession).where(OsceSession.user_id == user.id)
    ).scalars().all()
    for sitting in sittings:
        db.delete(sitting)
    db.commit()
    return {"cleared": len(sittings)}


@router.delete("/stations/{station_id}/attempts", status_code=status.HTTP_200_OK)
def clear_attempts(station_id: int, user: CurrentUser, db: DbSession) -> dict[str, int]:
    """Forget this candidate's attempts at a station so it can be sat again.

    Circuits never repeat a station that has been attempted, so this is how a
    candidate deliberately asks for one back. Only their own sittings go: the
    station stays closed for everyone else who has sat it.
    """
    sittings = db.execute(
        select(OsceSession).where(
            OsceSession.station_id == station_id, OsceSession.user_id == user.id
        )
    ).scalars().all()
    for sitting in sittings:
        db.delete(sitting)
    db.commit()
    return {"cleared": len(sittings)}


@router.post("/sittings/{session_id}/begin")
def begin_sitting(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    sitting = _load_sitting(db, session_id, user)
    if sitting.started_at is not None:
        raise HTTPException(status_code=400, detail="This station has already begun")
    sitting.started_at = datetime.now(timezone.utc)
    db.commit()
    return _clock(sitting).as_dict()


@router.get("/sittings/{session_id}/clock")
def sitting_clock(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    return _clock(_load_sitting(db, session_id, user)).as_dict()


@router.get("/sittings/{session_id}")
def get_sitting(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    sitting = _load_sitting(db, session_id, user)
    station = db.get(OsceStation, sitting.station_id)
    clock = _clock(sitting)

    responses = {
        r.prompt_label: r
        for r in db.execute(
            select(OsceResponse).where(OsceResponse.session_id == sitting.id)
        ).scalars().all()
    }

    by_id = {f.id: f for f in station.figures}
    prompts = []
    # Not every question the station holds is one worth asking. A station
    # that found no image states its findings instead, so opening with
    # "describe what you see" tests nothing and spends a minute doing it.
    for index, prompt in enumerate(sittable_prompts(station)):
        label = prompt.get("label") or str(index)
        response = responses.get(label)
        # A question may ask for two investigations - "the OCT and the
        # angiogram" - and no one image is both, so each has its own figure and
        # the question shows them together. `figure_ids` is the list;
        # `figure_id` alone is the older single binding, still the common case.
        shown = figures_for_prompt(by_id, prompt)
        prompts.append(
            {
                "label": label,
                "index": index,
                "text": prompt.get("text"),
                "seconds": prompt.get("seconds"),
                # The investigations this question asks them to read, shown only
                # once the question is reached.
                "figures": shown,
                "marks": sum(pt.get("marks", 0) for pt in (prompt.get("rubric") or [])),
                "transcript": response.transcript if response else None,
                "transcript_edited": response.transcript_edited if response else None,
                "transcription_status": response.transcription_status if response else "none",
                "transcription_error": response.transcription_error if response else None,
            }
        )

    # Only findings a real examiner would state are exposed during the sitting.
    # The elicited signs are the answer to every "describe what you see" prompt,
    # so they are withheld until the result. If a station has not been split
    # yet, nothing is shown rather than risk leaking it.
    if station.findings_split_status == "complete":
        given = station.findings_given
    else:
        given = None

    # An image belonging to a question is NOT shown with the patient: an MRI on
    # screen from the start answers the question before it is asked. It travels
    # with its own prompt instead, and appears when that prompt does.
    figures = opening_figures_payload(station)

    return {
        "id": sitting.id,
        "station": {
            "id": station.id,
            "subspecialty": station.subspecialty,
            "title": station.title,
            # Neither the case summary nor the history is shown: both name or
            # strongly imply the diagnosis. The candidate gets the patient in
            # front of them, the examiner's opening question, and the image -
            # which is what a real station gives them.
            "patient_demographic": station.patient_demographic,
            "findings_given": given,
            "findings_pending_split": station.findings_split_status != "complete",
            "figures": figures,
            "total_marks": station.total_marks,
        },
        "clock": clock.as_dict(),
        "current_prompt_index": sitting.current_prompt_index,
        "is_timed": sitting.is_timed,
        "submitted_at": sitting.submitted_at.isoformat() if sitting.submitted_at else None,
        "grading_status": sitting.grading_status,
        "prompts": prompts,
    }


@router.post("/sittings/{session_id}/answers", status_code=status.HTTP_201_CREATED)
async def upload_answer(
    session_id: int,
    user: CurrentUser,
    db: DbSession,
    prompt_label: str = Form(...),
    prompt_index: int = Form(default=0),
    duration_ms: int = Form(default=0),
    audio: UploadFile = File(...),
) -> dict[str, Any]:
    """Accept one recorded answer and queue it for transcription.

    Called the moment the candidate finishes a question, while they are already
    reading the next one, so transcription overlaps the next answer rather than
    stalling the station.
    """
    sitting = _load_sitting(db, session_id, user)
    if sitting.submitted_at is not None:
        raise HTTPException(status_code=409, detail="This station has been submitted")

    clock = _clock(sitting)
    if not clock.can_record:
        raise HTTPException(
            status_code=409, detail="The station clock has expired; this answer was not saved."
        )

    content_type = (audio.content_type or "").lower()
    if content_type and not content_type.startswith(ACCEPTED_AUDIO_PREFIXES):
        raise HTTPException(status_code=400, detail=f"Unsupported audio type '{content_type}'")

    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="The recording was empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Recording is {len(data) // 1024 // 1024} MB; the limit per answer "
                   f"is {MAX_AUDIO_BYTES // 1024 // 1024} MB.",
        )

    clip = AudioClip(
        sha256=hashlib.sha256(data).hexdigest(),
        # iOS Safari sends audio/mp4; Chromium sends audio/webm. Both are kept
        # verbatim and declared to the transcriber as-is.
        content_type=content_type or "audio/mp4",
        data=data,
        size_bytes=len(data),
        duration_ms=duration_ms or None,
    )
    db.add(clip)
    db.flush()

    response = db.execute(
        select(OsceResponse)
        .where(OsceResponse.session_id == sitting.id)
        .where(OsceResponse.prompt_label == prompt_label)
    ).scalar_one_or_none()
    if response is None:
        response = OsceResponse(
            session_id=sitting.id, prompt_label=prompt_label, prompt_index=prompt_index
        )
        db.add(response)
    response.audio_clip_id = clip.id
    response.duration_ms = duration_ms or None
    response.transcription_status = "pending"
    response.transcription_error = None

    sitting.current_prompt_index = max(sitting.current_prompt_index, prompt_index + 1)
    db.commit()
    db.refresh(response)

    job = create_job(
        db,
        JOB_TRANSCRIBE_RESPONSE,
        payload={"response_id": response.id},
        created_by_id=user.id,
        total_steps=1,
        message=f"Transcribing answer {prompt_label}",
    )
    return {"response_id": response.id, "job_id": job.id, "bytes": len(data)}


class UploadFailedRequest(BaseModel):
    reason: str = ""
    duration_ms: int = 0


@router.post("/sittings/{session_id}/answers/{prompt_label}/upload-failed",
             status_code=status.HTTP_204_NO_CONTENT)
def record_failed_upload(
    session_id: int,
    prompt_label: str,
    payload: UploadFailedRequest,
    user: CurrentUser,
    db: DbSession,
) -> None:
    """Record that an answer was given and never arrived.

    Marking treats a question with no response row as one the candidate
    skipped, and scores it zero - which is right, until the reason there is no
    row is that the upload failed. On 8 Aug a background job starved the
    instance for eighty seconds, answer C of a live station never landed, and
    it was marked 0 of 2.5 with "nothing was recorded" against a candidate who
    had answered it.

    The client is the only thing that knows. This is it saying so, and it is
    deliberately cheap: no audio, because the audio is what could not be sent.

    It never overwrites an answer that did arrive - a late retry that succeeds
    must not be undone by a failure report still in flight behind it.
    """
    sitting = _load_sitting(db, session_id, user)
    response = db.execute(
        select(OsceResponse)
        .where(OsceResponse.session_id == sitting.id)
        .where(OsceResponse.prompt_label == prompt_label)
    ).scalar_one_or_none()
    if response is not None and response.audio_clip_id is not None:
        return  # it arrived after all

    if response is None:
        response = OsceResponse(
            session_id=sitting.id,
            prompt_label=prompt_label,
            prompt_index=0,
            duration_ms=payload.duration_ms or None,
        )
        db.add(response)
    response.transcription_status = "failed"
    response.transcription_error = (
        f"The answer was recorded but never reached the server: "
        f"{payload.reason or 'upload failed'}"
    )
    db.commit()


class EditTranscriptRequest(BaseModel):
    transcript: str


@router.put("/sittings/{session_id}/answers/{prompt_label}/transcript")
def edit_transcript(
    session_id: int,
    prompt_label: str,
    payload: EditTranscriptRequest,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    """Correct a mis-heard transcript before marking."""
    sitting = _load_sitting(db, session_id, user)
    if sitting.grading_status in {"complete", "running"}:
        raise HTTPException(
            status_code=409,
            detail="This station has already been marked. Corrections must be made "
                   "before submitting.",
        )
    response = db.execute(
        select(OsceResponse)
        .where(OsceResponse.session_id == sitting.id)
        .where(OsceResponse.prompt_label == prompt_label)
    ).scalar_one_or_none()
    if response is None:
        raise HTTPException(status_code=404, detail="No answer recorded for that question")
    response.transcript_edited = payload.transcript
    db.commit()
    return {"prompt_label": prompt_label, "saved": True}


@router.post("/sittings/{session_id}/submit")
def submit_sitting(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    sitting = _load_sitting(db, session_id, user)
    if sitting.submitted_at is not None:
        raise HTTPException(status_code=400, detail="Already submitted")
    sitting.submitted_at = datetime.now(timezone.utc)
    db.commit()

    job_id = None
    if SettingsStore(db).get_bool("osce.auto_grade_on_submit", True):
        sitting.grading_status = "queued"
        db.commit()
        job_id = create_job(
            db, JOB_GRADE_OSCE, payload={"session_id": sitting.id},
            created_by_id=user.id, message="Marking station",
        ).id
    return {
        "submitted_at": sitting.submitted_at.isoformat(),
        "grading_job_id": job_id,
        # Where the candidate goes next. A circuit is nine stations in one
        # sitting of the mind: marking runs behind them and the result waits
        # until the end, exactly as it does on the day.
        "circuit": _circuit_next(db, sitting, user),
    }


@router.post("/sittings/{session_id}/grade", status_code=status.HTTP_202_ACCEPTED)
def grade_sitting(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    sitting = _load_sitting(db, session_id, user)
    if sitting.submitted_at is None:
        raise HTTPException(status_code=400, detail="This station has not been submitted")
    sitting.grading_status = "queued"
    db.commit()
    job = create_job(
        db, JOB_GRADE_OSCE, payload={"session_id": sitting.id},
        created_by_id=user.id, message="Marking station",
    )
    return {"job_id": job.id}


def _with_model_answers(
    breakdown: list[dict[str, Any]] | None, prompt: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """The marking breakdown, each point carrying the answer that earns it.

    Joined in from the station at read time rather than copied into the grade
    when it was written. The model answer belongs to the question - what a
    fundus shows does not depend on who was asked - so writing it once has to
    reach the sittings that are already over, which is most of them. It also
    means correcting an answer corrects every review of it.

    The grade's own text and marks are left exactly as they were: those are the
    key the candidate was actually marked against, and a rubric edited since
    must not rewrite the record of what happened.
    """
    if not breakdown:
        return breakdown
    rubric = prompt.get("rubric") or []
    out = []
    for item in breakdown:
        index = item.get("index")
        answer = None
        if isinstance(index, int) and 0 <= index < len(rubric):
            # Only when it is still the same point. A rubric rewritten since
            # the sitting would otherwise put one point's answer under another
            # point's text.
            if str(rubric[index].get("text") or "") == str(item.get("text") or ""):
                answer = str(rubric[index].get("model_answer") or "").strip() or None
        out.append({**item, "model_answer": answer})
    return out


@router.get("/sittings/{session_id}/result")
def sitting_result(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    sitting = _load_sitting(db, session_id, user)
    station = db.get(OsceStation, sitting.station_id)
    result = db.execute(
        select(OsceResult).where(OsceResult.session_id == sitting.id)
    ).scalar_one_or_none()

    responses = {
        r.prompt_label: r
        for r in db.execute(
            select(OsceResponse).where(OsceResponse.session_id == sitting.id)
        ).scalars().all()
    }

    # Every grade for this sitting in one read, rather than one query per
    # question per examiner pass.
    grades_by_label: dict[str, list[OsceGrade]] = {}
    for grade in db.execute(
        select(OsceGrade)
        .where(OsceGrade.session_id == sitting.id)
        .order_by(OsceGrade.examiner_pass)
    ).scalars().all():
        grades_by_label.setdefault(grade.prompt_label, []).append(grade)

    by_id = {f.id: f for f in station.figures}
    prompts = []
    for index, prompt in enumerate(station.prompts or []):
        label = prompt.get("label") or str(index)
        grades = grades_by_label.get(label, [])
        response = responses.get(label)
        awarded = (
            sum(g.awarded_marks for g in grades) / len(grades) if grades else None
        )
        prompts.append(
            {
                "label": label,
                "text": prompt.get("text"),
                "marks": sum(pt.get("marks", 0) for pt in (prompt.get("rubric") or [])),
                "awarded": round(awarded, 2) if awarded is not None else None,
                "transcript": response.marking_text if response else "",
                # Why the transcript could not be trusted, if it could not be.
                # A zero the transcriber caused reads exactly like a zero the
                # candidate earned unless the reason survives to the result.
                "transcription_error": response.transcription_error if response else None,
                # Exactly what was on screen when the question was asked, so a
                # mark can be read against the picture it was given for. A
                # rejected figure was never shown and is not shown here either.
                "figures": figures_for_prompt(by_id, prompt),
                "flagged": label in (result.flagged_prompts or []) if result else False,
                "examiners": [
                    {
                        "pass": g.examiner_pass,
                        "awarded": g.awarded_marks,
                        "feedback": g.feedback,
                        "breakdown": _with_model_answers(g.breakdown, prompt),
                    }
                    for g in grades
                ],
            }
        )

    return {
        "id": sitting.id,
        # Which circuit this was sat in, so the review can offer the way back to
        # it. Without this the only route out was the browser's back button.
        "circuit_id": sitting.circuit_id,
        "station": {
            "id": station.id,
            # How the paper itself names it - "1A", "13" - so a station being
            # discussed can be found in the report it came from.
            "station_number": station.station_number,
            "station_label": station.station_label,
            "exam_period": station.exam_period,
            "subspecialty": station.subspecialty,
            "title": station.title,
            "diagnosis": station.diagnosis,
            # Safe to reveal now the candidate has answered.
            "case_summary": station.case_summary,
            "patient_history": station.patient_history,
            "findings": station.findings,
            "findings_elicited": station.findings_elicited,
            "common_mistakes": station.common_mistakes,
            "cohort_performance": station.cohort_performance,
            "aims": station.aims,
            # The patient, as the candidate opened on them.
            "figures": opening_figures_payload(station),
        },
        "grading_status": sitting.grading_status,
        "result": {
            "total_awarded": result.total_awarded,
            "total_available": result.total_available,
            "percentage": result.percentage,
            "cut_score": result.cut_score,
            "outcome": result.outcome,
            "overall_feedback": result.overall_feedback,
            "flagged_prompts": result.flagged_prompts,
            "ungraded_prompts": result.ungraded_prompts,
        } if result else None,
        "prompts": prompts,
    }
