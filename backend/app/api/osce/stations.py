"""Stations and their figures: the admin side of the bank.

Everything here is about getting a station ready to be sat - building its
prompts, sourcing and reviewing its images, and deleting it."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import func, select
from app.api.deps import AdminUser, CurrentUser, DbSession
from app.constants import ROLE_ADMIN, SUBSPECIALTIES
from app.models import Image, OsceCircuit, OsceFigure, OsceSession, OsceStation
from app.services.jobs.runner import create_job
from app.services.osce.sittability import station_faults
from app.services.osce.findings import JOB_SPLIT_OSCE_FINDINGS, stations_needing_split
from app.services.osce.prompts import JOB_BUILD_OSCE_PROMPTS, stations_needing_prompts
from app.services.osce.station_images import (
    JOB_DESCRIBE_STATION_FIGURES,
    JOB_SETTLE_STATIONS,
    JOB_SOURCE_STATION_IMAGES,
    JOB_VERIFY_STATION_FIGURES,
    figures_needing_description,
    stations_needing_images,
    stations_with_unchecked_figures,
)
from app.api.osce.helpers import _all_bound_ids, _bound_figure_ids

router = APIRouter()


# --- Stations -------------------------------------------------------------
class StationSummary(BaseModel):
    id: int
    station_number: int | None
    # "1A" where the paper names its stations that way, otherwise null and the
    # number stands on its own.
    station_label: str | None
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
                station_label=station.station_label,
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
    # One new station in every subspecialty - nine, whatever the bank already
    # holds. Topping up to a target is a different question and answers "how
    # thin is my weakest area"; this answers "give me a fresh circuit's worth".
    one_each: bool = False


@router.post("/stations/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_stations(
    payload: GenerateStationsRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    from app.services.generate import JOB_GENERATE_STATIONS, thin_subspecialties

    if payload.one_each:
        wanted = {name: 1 for name in SUBSPECIALTIES}
    else:
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


class SourceImagesRequest(BaseModel):
    """Which stations to source for, and how many at once.

    Sourcing every station that needs one is a long run of searches and vision
    calls on a single free-tier instance, competing with whatever else is
    using it. Naming the stations, or capping the batch, lets it be done in
    pieces whose results can be looked at before spending on the next.
    """

    station_ids: list[int] | None = None
    limit: int | None = Field(default=None, ge=1, le=200)
    # Leave a station's own image alone when it is already a confident, approved
    # match, and spend only on the views and questions still without one.
    # Unset, it follows the intent: a sweep of the whole bank spends only on
    # what is missing, whereas naming a station is asking for it to be redone.
    only_missing: bool | None = None


@router.post("/stations/source-images", status_code=status.HTTP_202_ACCEPTED)
def source_images(
    admin: AdminUser, db: DbSession, payload: SourceImagesRequest | None = None
) -> dict[str, Any]:
    """Search for a clinical image per station and vision-verify each one."""
    payload = payload or SourceImagesRequest()
    ids = payload.station_ids or stations_needing_images(db)
    if payload.limit:
        ids = ids[: payload.limit]
    if not ids:
        raise HTTPException(status_code=400, detail="Every station already has a verified image")
    only_missing = (
        payload.only_missing if payload.only_missing is not None
        else payload.station_ids is None
    )
    job = create_job(
        db, JOB_SOURCE_STATION_IMAGES,
        payload={"station_ids": ids, "only_missing": only_missing},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Sourcing images for {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids), "only_missing": only_missing}


@router.post("/stations/settle", status_code=status.HTTP_202_ACCEPTED)
def settle_stations(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Bring every station's figures into line with the protocol as it stands.

    Runs itself at the end of every ingest. It is here as well because a rule
    that changes only applies where a figure is written, so stations built
    under an older one keep whatever it produced - which is why the same
    complaint kept returning after each fix.

    Spends nothing: no searching, no model calls, and running it twice changes
    nothing the first run did not.
    """
    ids = list(db.execute(select(OsceStation.id).order_by(OsceStation.id)).scalars().all())
    if not ids:
        raise HTTPException(status_code=400, detail="There are no stations")
    job = create_job(
        db, JOB_SETTLE_STATIONS, payload={"station_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Settling {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids)}


@router.post("/figures/recaption", status_code=status.HTTP_202_ACCEPTED)
def recaption_figures(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Describe every stored image again, with the station withheld.

    Captions written before the blind pass existed were produced by a
    verification that had been told what to expect and could agree without
    looking. Questions are now matched to their images by what the caption
    says, so an echoing caption hides the mismatch that check exists to find.

    Costs one vision call per figure. Nothing is rejected and no image is
    detached; a figure whose description disagrees with what was asked for is
    downgraded and the disagreement written beside it.
    """
    from app.services.osce.station_images.recaption import (
        JOB_RECAPTION_FIGURES,
        figures_needing_caption,
    )

    ids = figures_needing_caption(db)
    if not ids:
        raise HTTPException(status_code=400, detail="No figures carry an image")
    job = create_job(
        db, JOB_RECAPTION_FIGURES, payload={"figure_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Re-captioning {len(ids)} figure(s)",
    )
    return {"job_id": job.id, "figure_count": len(ids)}


@router.post("/stations/reconcile-questions", status_code=status.HTTP_202_ACCEPTED)
def reconcile_questions(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Make every question honest about the image that actually arrived.

    Runs itself at the end of every image-sourcing batch. It is here as well
    for the stations built before it existed, whose questions still promise
    images that were never found - a question is written before anyone knows
    whether its image can be had, and until this ran nothing looked again.

    Only questions that name something the candidate cannot see are touched,
    and the wording they had is kept so a rewrite can be undone.
    """
    from app.services.osce.reconcile import JOB_RECONCILE_QUESTIONS

    ids = list(db.execute(select(OsceStation.id).order_by(OsceStation.id)).scalars().all())
    if not ids:
        raise HTTPException(status_code=400, detail="There are no stations")
    job = create_job(
        db, JOB_RECONCILE_QUESTIONS, payload={"station_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Checking the questions on {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids)}


@router.post("/stations/recheck-figures", status_code=status.HTTP_202_ACCEPTED)
def recheck_station_figures(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Grade the papers' own photographs against their stations again.

    Ingest queues this itself, so it is normally not a decision anyone makes.
    It is here for the figures that were graded under a rule that has since
    changed: a real photograph of an investigation the opening task did not ask
    for used to be turned down, and the station then bought a web lookalike
    instead of showing the picture the paper already contained.

    Costs one vision call per figure and spends no image-search quota: nothing
    here searches for anything.
    """
    ids = stations_with_unchecked_figures(db)
    if not ids:
        raise HTTPException(
            status_code=400,
            detail="Every image taken from a paper has already been checked",
        )
    job = create_job(
        db, JOB_VERIFY_STATION_FIGURES,
        payload={"station_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Rechecking the figures of {len(ids)} station(s)",
    )
    return {"job_id": job.id, "station_count": len(ids)}


@router.post("/figures/describe-missing", status_code=status.HTTP_202_ACCEPTED)
def describe_missing_figures(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """State the findings in words for every view that has no image.

    The last resort of the protocol, run over the stations that have already
    reached it: sourcing found nothing usable, or what it found answered a
    different question and was turned down. Searching again is not this job -
    it spends no image-search quota and costs one model call per figure.
    """
    ids = figures_needing_description(db)
    if not ids:
        raise HTTPException(
            status_code=400, detail="Every station without an image already has its findings stated"
        )
    job = create_job(
        db, JOB_DESCRIBE_STATION_FIGURES,
        payload={"figure_ids": ids},
        created_by_id=admin.id, total_steps=len(ids),
        message=f"Describing {len(ids)} view(s) with no image",
    )
    return {"job_id": job.id, "figure_count": len(ids)}


class StationFigureOut(BaseModel):
    id: int
    station_id: int
    image_id: int | None
    caption: str | None
    # What this figure is FOR. A station can hold several, and without this
    # they render as identical cards headed by the same station and case -
    # a gaze montage, a CT angiogram and a third view looking like one figure
    # listed three times.
    wanted_description: str | None
    described_findings: str | None
    described_findings_approved: bool
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


@router.post("/figures/{figure_id}/approve-description", status_code=status.HTTP_204_NO_CONTENT)
def approve_figure_description(
    figure_id: int, admin: AdminUser, db: DbSession, approved: bool = True
) -> None:
    """Release a written description to candidates, or withdraw it.

    Separate from approving the image because a figure can carry both: a
    photograph that is fine and a description of what it cannot show. They are
    judged on different things and are approved separately.
    """
    figure = db.get(OsceFigure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")
    if approved and not figure.described_findings:
        raise HTTPException(status_code=400, detail="This figure has no description to approve")
    figure.described_findings_approved = approved
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
            # A question asking for two investigations holds a list, and losing
            # one of them must not unbind the other.
            remaining = [i for i in _bound_figure_ids(prompt) if i != figure.id]
            if remaining != _bound_figure_ids(prompt):
                prompt.pop("figure_id", None)
                prompt.pop("figure_ids", None)
                if remaining:
                    prompt["figure_id"] = remaining[0]
                    if len(remaining) > 1:
                        prompt["figure_ids"] = remaining
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


MAX_FIGURE_BYTES = 12 * 1024 * 1024


@router.post("/figures/{figure_id}/image", status_code=status.HTTP_201_CREATED)
async def upload_figure_image(
    figure_id: int,
    admin: AdminUser,
    db: DbSession,
    image: UploadFile = File(...),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    """Attach an image by hand, for a question no search can answer.

    Some investigations are simply not on the open web: an orthoptic Hess
    chart, a photograph of a forced duction test being performed, an A-scan
    biometry printout. Until now the pipeline could detach a figure's image but
    never attach one, so those questions had no way to be fixed at all - by
    anyone. A search that comes back empty needs somewhere to hand over to.

    A supplied image is trusted and shown at once. Nobody uploads a picture to
    a station by accident, and the vision grader exists to catch what a web
    search dragged in, not to second-guess the administrator who chose this
    one.
    """
    figure = db.get(OsceFigure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")

    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail=f"That is a {content_type or 'file of unknown type'}, not an image.",
        )
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file was empty")
    if len(data) > MAX_FIGURE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"That image is {len(data) / 1e6:.1f} MB; the limit is "
                   f"{MAX_FIGURE_BYTES // (1024 * 1024)} MB.",
        )

    digest = hashlib.sha256(data).hexdigest()
    # The same picture may already be in the bank, attached to another station.
    record = db.execute(select(Image).where(Image.sha256 == digest)).scalar_one_or_none()
    if record is None:
        record = Image(
            sha256=digest, content_type=content_type, data=data,
            size_bytes=len(data), origin="upload",
        )
        db.add(record)
        db.flush()

    figure.image_id = record.id
    figure.verification_status = "supplied"
    figure.is_approved = True
    figure.match_confidence = 1.0
    figure.verification_notes = f"Supplied by {admin.email}."
    if caption and caption.strip():
        figure.caption = caption.strip()
    # A description written because no image could be found is now beside the
    # point, and would be read out over the top of the picture.
    figure.described_findings = None
    figure.described_findings_approved = False
    db.commit()

    return {
        "figure_id": figure.id,
        "image_id": record.id,
        "caption": figure.caption,
        "size_bytes": len(data),
    }


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
        # Only what the candidate meets on entering. A figure bound to a
        # question travels with that question and appears when it does, so
        # listing it here made a motility station look as though it opened on
        # the MRI its question C asks about - which is exactly the complaint
        # this preview exists to catch.
        "figures": [
            {
                "id": f.id,
                "image_id": f.image_id,
                "caption": f.caption,
                "described_findings": f.described_findings,
                "described_findings_approved": f.described_findings_approved,
                "position": f.position,
                "is_approved": f.is_approved,
                "verification_status": f.verification_status,
                "shown_at": "start",
            }
            for f in sorted(station.figures, key=lambda f: f.position)
            if f.image_id and f.id not in _all_bound_ids(station.prompts or [])
        ],
        # Why a candidate could not answer this station in full, if anything.
        # The same judgement the audit and the sourcing selection use, so the
        # preview cannot say "fine" while the audit says otherwise.
        "faults": [
            {"kind": f.kind, "detail": f.detail, "fixable_by_sourcing": f.fixable_by_sourcing}
            for f in station_faults(station)
        ],
        "prompts": [
            {
                "label": p.get("label") or chr(ord("A") + i),
                "text": p.get("text"),
                "seconds": p.get("seconds"),
                "marks": sum(pt.get("marks", 0) for pt in (p.get("rubric") or [])),
                "rubric": p.get("rubric") or [],
                "figure_id": p.get("figure_id"),
                # Every figure the question carries: two investigations in one
                # question each get their own.
                "figure_ids": _bound_figure_ids(p),
                # Left set with no figure_id, this is a question asking for an
                # image that could not be found - the thing to fix by hand.
                "image_wanted": p.get("image_wanted"),
                # ...unless no search could ever fill it, in which case this
                # says why, and the request needs rewording rather than retrying.
                "image_impossible": p.get("image_impossible"),
            }
            for i, p in enumerate(station.prompts or [])
        ],
    }
