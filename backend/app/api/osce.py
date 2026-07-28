"""OSCE circuits, station sittings, spoken answers and results."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, DbSession, load_owned
from app.constants import ROLE_ADMIN
from app.models import (
    AudioClip,
    Image,
    OsceCircuit,
    OsceFigure,
    OsceGrade,
    OsceResponse,
    OsceResult,
    OsceSession,
    OsceStation,
)
from app.services.jobs.runner import create_job
from app.services.osce.circuit import (
    JOB_GRADE_OSCE,
    build_circuit,
    circuit_progress,
    compute_station_clock,
)
from app.services.osce.findings import JOB_SPLIT_OSCE_FINDINGS, stations_needing_split
from app.services.osce.prompts import JOB_BUILD_OSCE_PROMPTS, stations_needing_prompts
from app.services.osce.station_images import (
    JOB_SOURCE_STATION_IMAGES,
    stations_needing_images,
)
from app.services.osce.transcribe_job import JOB_TRANSCRIBE_RESPONSE
from app.services.settings_store import SettingsStore

router = APIRouter(prefix="/osce", tags=["osce"])

# A single spoken answer. Generous enough for a rambling 3-minute reply,
# tight enough to reject a runaway recording.
MAX_AUDIO_BYTES = 15 * 1024 * 1024
ACCEPTED_AUDIO_PREFIXES = ("audio/", "video/mp4", "video/webm")


# --- Stations -------------------------------------------------------------
class StationSummary(BaseModel):
    id: int
    station_number: int | None
    subspecialty: str | None
    title: str | None
    # Administrators only. The summary names or strongly implies the diagnosis,
    # and the browse list used it as a fallback station name, so a candidate
    # scrolling the list read the case before choosing to sit it. Admins need it
    # to tell one station from another when reviewing images.
    case_summary: str | None
    exam_period: str | None
    # "past_paper" or "generated" - the list marks which, because a circuit
    # mixes the two and it is not otherwise apparent which you are sitting.
    source: str | None
    total_marks: int
    prompt_count: int = 0
    prompts_status: str
    # Whether the VA/IOP numbers have been separated from the signs the
    # candidate must find. Until they are, a station opens with no data.
    findings_split_status: str = "none"
    has_given_findings: bool = False
    attempted: bool = False
    # This candidate's attempts only - one user's practice must not close a
    # station off for anyone else.
    attempt_count: int = 0
    last_attempt_at: datetime | None = None


@router.get("/stations", response_model=list[StationSummary])
def list_stations(user: CurrentUser, db: DbSession) -> list[StationSummary]:
    stations = db.execute(select(OsceStation).order_by(OsceStation.id)).scalars().all()
    counts = {
        station_id: (n, last)
        for station_id, n, last in db.execute(
            select(
                OsceSession.station_id,
                func.count(OsceSession.id),
                func.max(OsceSession.created_at),
            )
            .where(OsceSession.user_id == user.id)
            .group_by(OsceSession.station_id)
        ).all()
    }
    is_admin = user.role == ROLE_ADMIN
    out = []
    for station in stations:
        out.append(
            StationSummary(
                id=station.id,
                station_number=station.station_number,
                subspecialty=station.subspecialty,
                title=station.title,
                case_summary=station.case_summary if is_admin else None,
                exam_period=station.exam_period,
                source=station.source,
                total_marks=station.total_marks,
                prompt_count=len(station.prompts or []),
                prompts_status=station.prompts_status,
                findings_split_status=station.findings_split_status,
                has_given_findings=bool(station.findings_given),
                attempted=station.id in counts,
                attempt_count=counts.get(station.id, (0, None))[0],
                last_attempt_at=counts.get(station.id, (0, None))[1],
            )
        )
    return out


@router.post("/stations/build-prompts", status_code=status.HTTP_202_ACCEPTED)
def build_prompts(admin: AdminUser, db: DbSession, force: bool = False) -> dict[str, Any]:
    """Turn flat stations into timed examiner question sequences.

    `force` rebuilds stations that already have prompts. Needed when the way a
    station opens changes: the wording is baked in at build time, so existing
    stations keep the old opening until they are built again.
    """
    if force:
        ids = list(
            db.execute(select(OsceStation.id).order_by(OsceStation.id)).scalars().all()
        )
    else:
        ids = stations_needing_prompts(db)
    if not ids:
        raise HTTPException(status_code=400, detail="Every station already has prompts")
    job = create_job(
        db, JOB_BUILD_OSCE_PROMPTS, payload={"station_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Preparing {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids)}


class GenerateStationsRequest(BaseModel):
    """Top every subspecialty up to `target` stations, or request specific counts."""

    target_per_subspecialty: int = Field(default=6, ge=1, le=30)
    per_subspecialty: dict[str, int] | None = None
    difficulty: str | None = None


@router.post("/stations/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_stations(
    payload: GenerateStationsRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    from app.services.generate import JOB_GENERATE_STATIONS, thin_subspecialties

    wanted = payload.per_subspecialty or thin_subspecialties(
        db, payload.target_per_subspecialty
    )
    wanted = {k: v for k, v in wanted.items() if v > 0}
    if not wanted:
        raise HTTPException(
            status_code=400,
            detail=f"Every subspecialty already has at least "
                   f"{payload.target_per_subspecialty} stations.",
        )

    total = sum(wanted.values())
    job = create_job(
        db, JOB_GENERATE_STATIONS,
        payload={"per_subspecialty": wanted, "difficulty": payload.difficulty},
        created_by_id=admin.id, total_steps=total,
        message=f"Generating {total} station(s)",
    )
    return {"job_id": job.id, "plan": wanted, "total": total}


@router.post("/stations/split-findings", status_code=status.HTTP_202_ACCEPTED)
def split_findings_endpoint(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Separate findings an examiner states from signs the candidate must find."""
    ids = stations_needing_split(db)
    if not ids:
        raise HTTPException(status_code=400, detail="Every station has already been split")
    job = create_job(
        db, JOB_SPLIT_OSCE_FINDINGS, payload={"station_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Splitting findings for {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids)}


@router.post("/stations/source-images", status_code=status.HTTP_202_ACCEPTED)
def source_images(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Search for a clinical image per station and vision-verify each one."""
    ids = stations_needing_images(db)
    if not ids:
        raise HTTPException(status_code=400, detail="Every station already has a verified image")
    job = create_job(
        db, JOB_SOURCE_STATION_IMAGES, payload={"station_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Sourcing images for {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids)}


class StationFigureOut(BaseModel):
    id: int
    station_id: int
    image_id: int | None
    caption: str | None
    search_query: str | None
    verification_status: str
    verification_notes: str | None
    match_confidence: float | None
    is_approved: bool
    rejection_count: int = 0


@router.get("/figures", response_model=list[StationFigureOut])
def list_station_figures(admin: AdminUser, db: DbSession) -> list[StationFigureOut]:
    """Review queue for sourced images."""
    figures = db.execute(select(OsceFigure).order_by(OsceFigure.station_id)).scalars().all()
    return [StationFigureOut.model_validate(f, from_attributes=True) for f in figures]


@router.post("/figures/{figure_id}/approve", status_code=status.HTTP_204_NO_CONTENT)
def approve_figure(figure_id: int, admin: AdminUser, db: DbSession, approved: bool = True) -> None:
    figure = db.get(OsceFigure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")
    if approved and figure.image_id is None:
        raise HTTPException(status_code=400, detail="This figure has no image to approve")
    figure.is_approved = approved
    db.commit()


@router.post("/figures/{figure_id}/reject", status_code=status.HTTP_202_ACCEPTED)
def reject_figure_image(
    figure_id: int, admin: AdminUser, db: DbSession, find_replacement: bool = True
) -> dict[str, Any]:
    """Reject the current image and, by default, go and find another.

    The rejected source URL is remembered so a replacement search cannot hand
    back the same picture.
    """
    from app.services.osce.station_images import JOB_SOURCE_STATION_IMAGES

    figure = db.get(OsceFigure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")

    rejected = list(figure.rejected_urls or [])
    if figure.image_id:
        image = db.get(Image, figure.image_id)
        if image and image.source_url and image.source_url not in rejected:
            rejected.append(image.source_url)

    figure.rejected_urls = rejected
    figure.rejection_count = (figure.rejection_count or 0) + 1
    figure.image_id = None
    figure.is_approved = False
    figure.verification_status = "rejected"
    figure.verification_notes = (
        f"Rejected by the administrator ({figure.rejection_count} so far)."
    )
    db.commit()

    job_id = None
    if find_replacement:
        job = create_job(
            db, JOB_SOURCE_STATION_IMAGES,
            payload={"station_ids": [figure.station_id]},
            created_by_id=admin.id, total_steps=1,
            message="Finding a replacement image",
        )
        job_id = job.id

    return {
        "figure_id": figure.id,
        "rejected_so_far": len(rejected),
        "job_id": job_id,
    }


class AddFigureRequest(BaseModel):
    wanted_description: str = Field(min_length=3, max_length=500)
    source_now: bool = True


@router.post("/stations/{station_id}/figures", status_code=status.HTTP_201_CREATED)
def add_station_figure(
    station_id: int, payload: AddFigureRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Ask for one more image of the station, described by hand.

    Automatic coverage groups the rubric by eye, which is right for most
    stations and wrong for some. This is how an administrator adds the view it
    did not think of — "gonioscopy of the right angle showing the tube".
    """
    from app.services.osce.station_images import JOB_SOURCE_STATION_IMAGES

    station = db.get(OsceStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")

    figure = OsceFigure(
        station_id=station.id,
        position=len(station.figures) + 1,
        wanted_description=payload.wanted_description.strip(),
        verification_status="pending",
    )
    db.add(figure)
    db.commit()
    db.refresh(figure)

    job_id = None
    if payload.source_now:
        job = create_job(
            db, JOB_SOURCE_STATION_IMAGES,
            payload={"station_ids": [station.id]},
            created_by_id=admin.id, total_steps=1,
            message="Finding the requested image",
        )
        job_id = job.id

    return {"figure_id": figure.id, "job_id": job_id}


@router.delete("/figures/{figure_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_figure(figure_id: int, admin: AdminUser, db: DbSession) -> None:
    """Remove a figure entirely, rather than just detaching its image.

    A question bound to this figure would otherwise ask the candidate to read
    something that is no longer there, so the binding goes with it.
    """
    figure = db.get(OsceFigure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")

    station = db.get(OsceStation, figure.station_id)
    if station is not None and station.prompts:
        prompts = [dict(p) for p in station.prompts]
        changed = False
        for prompt in prompts:
            if prompt.get("figure_id") == figure.id:
                prompt.pop("figure_id", None)
                changed = True
        if changed:
            station.prompts = prompts
            flag_modified(station, "prompts")

    db.delete(figure)
    db.commit()


@router.delete("/figures/{figure_id}/image", status_code=status.HTTP_204_NO_CONTENT)
def detach_figure_image(figure_id: int, admin: AdminUser, db: DbSession) -> None:
    """Remove an image and leave the station without one."""
    figure = db.get(OsceFigure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")
    figure.image_id = None
    figure.is_approved = False
    figure.verification_status = "rejected"
    db.commit()


# --- Circuits -------------------------------------------------------------
class CircuitOut(BaseModel):
    id: int
    title: str
    scheduled_for: date | None
    station_ids: list[int]
    status: str
    progress: dict[str, Any] = {}
    created_at: datetime


class CreateCircuitRequest(BaseModel):
    station_count: int = Field(default=9, ge=1, le=18)
    scheduled_for: date | None = None


@router.get("/circuits", response_model=list[CircuitOut])
def list_circuits(user: CurrentUser, db: DbSession) -> list[CircuitOut]:
    stmt = select(OsceCircuit).order_by(OsceCircuit.id.desc())
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(OsceCircuit.user_id == user.id)
    circuits = db.execute(stmt).scalars().all()
    return [
        CircuitOut(
            id=c.id, title=c.title, scheduled_for=c.scheduled_for,
            station_ids=c.station_ids or [], status=c.status,
            progress=circuit_progress(db, c), created_at=c.created_at,
        )
        for c in circuits
    ]


@router.post("/circuits", response_model=CircuitOut, status_code=status.HTTP_201_CREATED)
def create_circuit(
    payload: CreateCircuitRequest, user: CurrentUser, db: DbSession
) -> CircuitOut:
    count = payload.station_count or SettingsStore(db).get_int("osce.stations_per_circuit", 9)
    try:
        circuit = build_circuit(db, user.id, count, payload.scheduled_for)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CircuitOut(
        id=circuit.id, title=circuit.title, scheduled_for=circuit.scheduled_for,
        station_ids=circuit.station_ids or [], status=circuit.status,
        progress=circuit_progress(db, circuit), created_at=circuit.created_at,
    )


# --- Sittings -------------------------------------------------------------
class StartSittingRequest(BaseModel):
    station_id: int
    circuit_id: int | None = None
    is_timed: bool = True


def _load_sitting(db: DbSession, session_id: int, user) -> OsceSession:
    return load_owned(db, OsceSession, session_id, user)


def _clock(sitting: OsceSession):
    return compute_station_clock(
        sitting.started_at, sitting.submitted_at, sitting.is_timed
    )


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


@router.delete("/stations/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(
    station_id: int, admin: AdminUser, db: DbSession, delete_sittings: bool = False
) -> None:
    """Remove a station outright — for one that ingested badly.

    Sittings are refused rather than silently destroyed, because deleting a
    station takes every candidate's recorded answers and marks with it.
    """
    station = db.get(OsceStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")

    sittings = db.execute(
        select(OsceSession).where(OsceSession.station_id == station_id)
    ).scalars().all()
    if sittings and not delete_sittings:
        raise HTTPException(
            status_code=409,
            detail=f"{len(sittings)} candidate sitting(s) exist for this station. Pass "
                   f"delete_sittings=true to remove them as well.",
        )

    # Circuits hold station ids in a JSON list rather than a foreign key, so
    # nothing else would drop the reference and the circuit would break when sat.
    for circuit in db.execute(select(OsceCircuit)).scalars().all():
        remaining = [i for i in (circuit.station_ids or []) if i != station_id]
        if len(remaining) != len(circuit.station_ids or []):
            circuit.station_ids = remaining

    db.delete(station)
    db.commit()


@router.get("/stations/{station_id}/preview")
def preview_station(station_id: int, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """The whole station as written, for reviewing it without sitting it.

    Admin only, and deliberately so: this is every question with its marking
    rubric and the diagnosis, which is exactly what a candidate must not see.
    """
    station = db.get(OsceStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")

    return {
        "id": station.id,
        "title": station.title,
        "subspecialty": station.subspecialty,
        "exam_period": station.exam_period,
        "source": station.source,
        "patient_demographic": station.patient_demographic,
        "findings_given": station.findings_given,
        "findings_elicited": station.findings_elicited,
        "diagnosis": station.diagnosis,
        "total_marks": station.total_marks,
        # Unapproved ones are included too, with their status: reviewing a
        # station is exactly when you want to see an image that is not showing.
        "figures": [
            {
                "id": f.id,
                "image_id": f.image_id,
                "caption": f.caption,
                "position": f.position,
                "is_approved": f.is_approved,
                "verification_status": f.verification_status,
            }
            for f in sorted(station.figures, key=lambda f: f.position)
            if f.image_id
        ],
        "prompts": [
            {
                "label": p.get("label") or chr(ord("A") + i),
                "text": p.get("text"),
                "seconds": p.get("seconds"),
                "marks": sum(pt.get("marks", 0) for pt in (p.get("rubric") or [])),
                "rubric": p.get("rubric") or [],
                "figure_id": p.get("figure_id"),
                # Left set with no figure_id, this is a question asking for an
                # image that could not be found - the thing to fix by hand.
                "image_wanted": p.get("image_wanted"),
            }
            for i, p in enumerate(station.prompts or [])
        ],
    }


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
    for index, prompt in enumerate(station.prompts or []):
        label = prompt.get("label") or str(index)
        response = responses.get(label)
        figure = by_id.get(prompt.get("figure_id"))
        prompts.append(
            {
                "label": label,
                "index": index,
                "text": prompt.get("text"),
                "seconds": prompt.get("seconds"),
                # The investigation this question asks them to read, shown only
                # once the question is reached.
                "figure": (
                    {"id": figure.id, "image_id": figure.image_id, "caption": figure.caption}
                    if figure and figure.image_id and figure.is_approved
                    else None
                ),
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
    prompt_figure_ids = {
        p.get("figure_id") for p in (station.prompts or []) if p.get("figure_id")
    }
    figures = [
        {
            "id": f.id,
            "image_id": f.image_id,
            "caption": f.caption,
            "position": f.position,
        }
        for f in sorted(station.figures, key=lambda f: f.position)
        if f.image_id and f.is_approved and f.id not in prompt_figure_ids
    ]

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
    return {"submitted_at": sitting.submitted_at.isoformat(), "grading_job_id": job_id}


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
                "flagged": label in (result.flagged_prompts or []) if result else False,
                "examiners": [
                    {
                        "pass": g.examiner_pass,
                        "awarded": g.awarded_marks,
                        "feedback": g.feedback,
                        "breakdown": g.breakdown,
                    }
                    for g in grades
                ],
            }
        )

    return {
        "id": sitting.id,
        "station": {
            "id": station.id,
            "subspecialty": station.subspecialty,
            "title": station.title,
            "diagnosis": station.diagnosis,
            # Safe to reveal now the candidate has answered.
            "case_summary": station.case_summary,
            "patient_history": station.patient_history,
            "findings": station.findings,
            "findings_elicited": station.findings_elicited,
            "common_mistakes": station.common_mistakes,
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
