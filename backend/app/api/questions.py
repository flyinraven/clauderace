"""Question bank: browse, inspect, edit, and generate model answers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.constants import (
    QUESTION_SEQ,
    QUESTION_VSAQ,
    STATUS_APPROVED,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_REVIEW,
)
from app.models import (
    Figure,
    Image,
    ModelAnswerPoint,
    Question,
    QuestionPart,
)
from app.services.answers.generate import JOB_GENERATE_MODEL_ANSWERS
from app.services.jobs.runner import create_job

router = APIRouter(tags=["questions"])

VALID_STATUSES = {STATUS_DRAFT, STATUS_REVIEW, STATUS_APPROVED, STATUS_ARCHIVED}


# --- Response shapes ------------------------------------------------------
class AnswerPointOut(BaseModel):
    id: int
    text: str
    marks: float
    is_critical: bool
    from_examiner_feedback: bool
    rationale: str | None
    accepted_alternatives: list[str] | None

    model_config = {"from_attributes": True}


class PartOut(BaseModel):
    id: int
    label: str | None
    position: int
    text: str
    marks: float
    preamble: str | None
    answer_points: list[AnswerPointOut] = []

    model_config = {"from_attributes": True}


class FigureOut(BaseModel):
    id: int
    label: str | None
    caption: str | None
    position: int
    image_id: int | None
    wanted_description: str | None
    image_description: str | None = None
    source_url: str | None = None

    model_config = {"from_attributes": True}


class FeedbackOut(BaseModel):
    examiner_number: int | None
    common_mistakes: list[str] | None
    cohort_impression: list[str] | None

    model_config = {"from_attributes": True}


class QuestionSummary(BaseModel):
    id: int
    question_type: str
    subspecialty: str | None
    topic: str | None
    total_marks: int
    status: str
    source: str
    exam_period: str | None
    original_number: int | None
    model_answer_status: str
    part_count: int = 0
    figure_count: int = 0
    angoff_expected: float | None
    created_at: datetime

    model_config = {"from_attributes": True}


class QuestionDetail(QuestionSummary):
    purpose: str | None
    stem: str
    curriculum_standard_raw: str | None
    curriculum_codes: list[str] | None
    angoff_rationale: str | None
    generation_meta: dict[str, Any] | None
    parts: list[PartOut] = []
    figures: list[FigureOut] = []
    examiner_feedback: list[FeedbackOut] = []


class QuestionPage(BaseModel):
    items: list[QuestionSummary]
    total: int
    limit: int
    offset: int


# --- Browse ---------------------------------------------------------------
@router.get("/questions", response_model=QuestionPage)
def list_questions(
    user: CurrentUser,
    db: DbSession,
    question_type: str | None = None,
    subspecialty: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    source: str | None = None,
    exam_period: str | None = None,
    model_answer_status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
) -> QuestionPage:
    stmt = select(Question)
    count_stmt = select(func.count(Question.id))

    filters = []
    if question_type:
        filters.append(Question.question_type == question_type.upper())
    if subspecialty:
        filters.append(Question.subspecialty == subspecialty)
    if status_filter:
        filters.append(Question.status == status_filter)
    if source:
        filters.append(Question.source == source)
    if exam_period:
        filters.append(Question.exam_period == exam_period)
    if model_answer_status:
        filters.append(Question.model_answer_status == model_answer_status)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(Question.topic.ilike(pattern), Question.stem.ilike(pattern))
        )
    # Candidates only ever see approved material.
    if not user.is_admin:
        filters.append(Question.status == STATUS_APPROVED)

    for condition in filters:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = int(db.execute(count_stmt).scalar_one() or 0)
    rows = db.execute(
        stmt.order_by(Question.id.desc()).limit(limit).offset(offset)
    ).scalars().all()

    items: list[QuestionSummary] = []
    for question in rows:
        summary = QuestionSummary.model_validate(question)
        summary.part_count = len(question.parts)
        summary.figure_count = len(question.figures)
        items.append(summary)

    return QuestionPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/questions/{question_id}", response_model=QuestionDetail)
def get_question(question_id: int, user: CurrentUser, db: DbSession) -> QuestionDetail:
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if not user.is_admin and question.status != STATUS_APPROVED:
        raise HTTPException(status_code=404, detail="Question not found")

    detail = QuestionDetail.model_validate(question)
    detail.part_count = len(question.parts)
    detail.figure_count = len(question.figures)
    detail.parts = [
        PartOut(
            id=part.id,
            label=part.label,
            position=part.position,
            text=part.text,
            marks=part.marks,
            preamble=part.preamble,
            answer_points=[AnswerPointOut.model_validate(p) for p in part.answer_points],
        )
        for part in sorted(question.parts, key=lambda p: p.position)
    ]
    detail.figures = [
        _figure_out(db, figure) for figure in sorted(question.figures, key=lambda f: f.position)
    ]
    detail.examiner_feedback = [
        FeedbackOut.model_validate(f) for f in question.examiner_feedback
    ]
    return detail


def _figure_out(db, figure: Figure) -> FigureOut:
    out = FigureOut.model_validate(figure)
    if figure.image_id:
        image = db.get(Image, figure.image_id)
        if image:
            out.image_description = image.ai_description
            out.source_url = image.source_url
    return out


# --- Editing --------------------------------------------------------------
class QuestionUpdate(BaseModel):
    topic: str | None = None
    subspecialty: str | None = None
    purpose: str | None = None
    stem: str | None = None
    status: str | None = None
    difficulty: str | None = None
    angoff_expected: float | None = Field(default=None, ge=0, le=1)


@router.patch("/questions/{question_id}", response_model=QuestionDetail)
def update_question(
    question_id: int, payload: QuestionUpdate, admin: AdminUser, db: DbSession
) -> QuestionDetail:
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if payload.status is not None and payload.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"Status must be one of {sorted(VALID_STATUSES)}"
        )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(question, field, value)
    db.commit()
    return get_question(question_id, admin, db)


class BulkStatusRequest(BaseModel):
    question_ids: list[int] | None = None
    # When no ids are given, apply to everything matching these filters.
    from_status: str | None = None
    question_type: str | None = None
    source: str | None = None
    exam_period: str | None = None
    require_model_answer: bool = False
    to_status: str


@router.post("/questions/bulk-status")
def bulk_update_status(
    payload: BulkStatusRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Move many questions through the review workflow at once.

    Reviewing 36 transcribed questions one at a time is not a good use of an
    examiner's time, so this applies a status change to a filtered set.
    """
    if payload.to_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"Status must be one of {sorted(VALID_STATUSES)}"
        )

    stmt = select(Question)
    if payload.question_ids:
        stmt = stmt.where(Question.id.in_(payload.question_ids))
    else:
        if payload.from_status:
            stmt = stmt.where(Question.status == payload.from_status)
        if payload.question_type:
            stmt = stmt.where(Question.question_type == payload.question_type.upper())
        if payload.source:
            stmt = stmt.where(Question.source == payload.source)
        if payload.exam_period:
            stmt = stmt.where(Question.exam_period == payload.exam_period)
    if payload.require_model_answer:
        stmt = stmt.where(Question.model_answer_status == "complete")

    questions = db.execute(stmt).scalars().all()
    for question in questions:
        question.status = payload.to_status
    db.commit()
    return {"updated": len(questions), "to_status": payload.to_status}


class PartUpdate(BaseModel):
    text: str | None = None
    marks: float | None = Field(default=None, ge=0)
    preamble: str | None = None


@router.patch("/parts/{part_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_part(part_id: int, payload: PartUpdate, admin: AdminUser, db: DbSession) -> None:
    part = db.get(QuestionPart, part_id)
    if part is None:
        raise HTTPException(status_code=404, detail="Sub-question not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(part, field, value)
    question = db.get(Question, part.question_id)
    if question:
        question.total_marks = int(sum(p.marks for p in question.parts))
    db.commit()


class AnswerPointUpdate(BaseModel):
    text: str | None = None
    marks: float | None = Field(default=None, ge=0)
    is_critical: bool | None = None
    rationale: str | None = None


@router.patch("/answer-points/{point_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_answer_point(
    point_id: int, payload: AnswerPointUpdate, admin: AdminUser, db: DbSession
) -> None:
    point = db.get(ModelAnswerPoint, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail="Key point not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(point, field, value)
    db.commit()


@router.delete("/answer-points/{point_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_answer_point(point_id: int, admin: AdminUser, db: DbSession) -> None:
    point = db.get(ModelAnswerPoint, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail="Key point not found")
    db.delete(point)
    db.commit()


# --- Model answer generation ---------------------------------------------
class GenerateAnswersRequest(BaseModel):
    question_ids: list[int] | None = None
    only_missing: bool = True
    limit: int | None = Field(default=None, ge=1, le=500)


@router.post("/questions/generate-model-answers", status_code=status.HTTP_202_ACCEPTED)
def generate_model_answers(
    payload: GenerateAnswersRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    if payload.question_ids:
        ids = payload.question_ids
    else:
        stmt = select(Question.id).order_by(Question.id)
        if payload.only_missing:
            stmt = stmt.where(Question.model_answer_status.in_(["none", "failed"]))
        if payload.limit:
            stmt = stmt.limit(payload.limit)
        ids = list(db.execute(stmt).scalars().all())

    if not ids:
        raise HTTPException(status_code=400, detail="No questions matched")

    job = create_job(
        db,
        JOB_GENERATE_MODEL_ANSWERS,
        payload={"question_ids": ids},
        created_by_id=admin.id,
        total_steps=len(ids),
        message=f"Generating model answers for {len(ids)} question(s)",
    )
    return {"job_id": job.id, "question_count": len(ids)}


# --- Question generation --------------------------------------------------
class GenerateQuestionsRequest(BaseModel):
    question_type: str = "VSAQ"
    count: int = Field(default=15, ge=1, le=200)
    subspecialties: list[str] | None = None
    difficulty: str | None = None


@router.post("/questions/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_questions(
    payload: GenerateQuestionsRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    from app.constants import SUBSPECIALTIES
    from app.services.generate import JOB_GENERATE_QUESTIONS

    question_type = payload.question_type.upper()
    if question_type not in {"SEQ", "VSAQ"}:
        raise HTTPException(status_code=400, detail="question_type must be SEQ or VSAQ")

    # Default to cycling all nine subspecialties so a batch is balanced.
    subspecialties = payload.subspecialties or list(SUBSPECIALTIES)
    unknown = [s for s in subspecialties if s not in SUBSPECIALTIES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown subspecialty: {unknown[0]}")

    job = create_job(
        db,
        JOB_GENERATE_QUESTIONS,
        payload={
            "question_type": question_type,
            "count": payload.count,
            "subspecialties": subspecialties,
            "difficulty": payload.difficulty,
        },
        created_by_id=admin.id,
        message=f"Generating {payload.count} {question_type}(s)",
    )
    return {"job_id": job.id, "count": payload.count, "question_type": question_type}


# --- Image search ---------------------------------------------------------
class AttachImagesRequest(BaseModel):
    question_id: int | None = None
    figure_ids: list[int] | None = None


@router.post("/figures/attach-images", status_code=status.HTTP_202_ACCEPTED)
def attach_images(
    payload: AttachImagesRequest, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    from app.services.imagesearch import (
        JOB_ATTACH_IMAGES,
        figures_needing_images,
        quota_status,
    )

    ids = payload.figure_ids or figures_needing_images(db, payload.question_id)
    if not ids:
        raise HTTPException(status_code=400, detail="No figures are missing an image")

    quota = quota_status(db)
    if quota["limit"] > 0 and len(ids) > quota["remaining"]:
        raise HTTPException(
            status_code=400,
            detail=f"{len(ids)} searches needed but only {quota['remaining']} remain "
                   f"in this month's limit of {quota['limit']}. Raise the limit in "
                   f"Admin > Settings or select fewer figures.",
        )

    job = create_job(
        db,
        JOB_ATTACH_IMAGES,
        payload={"figure_ids": ids},
        created_by_id=admin.id,
        total_steps=len(ids),
        message=f"Searching for {len(ids)} image(s)",
    )
    return {"job_id": job.id, "figure_count": len(ids)}


@router.get("/figures/image-quota")
def image_quota(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    from app.services.imagesearch import quota_status

    return quota_status(db)


@router.post("/figures/{figure_id}/approve-image", status_code=status.HTTP_204_NO_CONTENT)
def approve_image(figure_id: int, admin: AdminUser, db: DbSession) -> None:
    """Approve a web-sourced image so candidates can see it."""
    figure = db.get(Figure, figure_id)
    if figure is None or figure.image_id is None:
        raise HTTPException(status_code=404, detail="Figure has no image")
    image = db.get(Image, figure.image_id)
    if image:
        image.is_approved = True
        db.commit()


@router.delete("/figures/{figure_id}/image", status_code=status.HTTP_204_NO_CONTENT)
def detach_image(figure_id: int, admin: AdminUser, db: DbSession) -> None:
    """Remove an unsuitable image, leaving the figure placeholder in place."""
    figure = db.get(Figure, figure_id)
    if figure is None:
        raise HTTPException(status_code=404, detail="Figure not found")
    figure.image_id = None
    db.commit()


# --- Images ---------------------------------------------------------------
@router.get("/images/{image_id}")
def get_image(image_id: int, user: CurrentUser, db: DbSession) -> Response:
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers={
            # Images are immutable once stored (the id is content-addressed via
            # sha256), so they can be cached hard.
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{image.sha256}"',
        },
    )


# --- Reference data -------------------------------------------------------
@router.get("/meta/filters")
def filter_options(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    from app.constants import SUBSPECIALTIES

    periods = [
        p for p in db.execute(
            select(Question.exam_period).distinct().where(Question.exam_period.is_not(None))
        ).scalars().all()
    ]
    return {
        "subspecialties": SUBSPECIALTIES,
        # OSCE stations are not questions - they live in osce_stations with
        # their own prompts and findings, and are browsed from the OSCE page.
        # Offering the type here only ever returned an empty bank.
        "question_types": [QUESTION_SEQ, QUESTION_VSAQ],
        "statuses": sorted(VALID_STATUSES),
        "sources": ["past_paper", "generated", "manual"],
        "exam_periods": sorted(periods),
    }
