"""Exam papers and timed sittings.

Content is gated by the server-side clock: during the preparation phase the
paper is not returned at all, during reading it is returned but answer saves
are rejected, and only during writing are edits accepted. The client is never
trusted to enforce any of this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import AdminUser, CurrentUser, DbSession, load_owned
from app.constants import (
    PAPER_SPECS,
    PHASE_NOT_STARTED,
    PHASE_SUBMITTED,
    ROLE_ADMIN,
)
from app.models import (
    Answer,
    ExamPaper,
    ExamPaperQuestion,
    ExamSession,
    Grade,
    Question,
    QuestionPart,
    SessionResult,
)
from app.services.exams import (
    AssemblyError,
    accepts_writes,
    assemble_paper,
    available_counts,
    compute_clock,
    spec_for_paper,
)
from app.services.grading import JOB_GRADE_SESSION
from app.services.jobs.runner import create_job
from app.services.settings_store import SettingsStore

router = APIRouter(tags=["exams"])


# --- Papers ---------------------------------------------------------------
class PaperOut(BaseModel):
    id: int
    title: str
    paper_type: str
    day: int | None
    paper_number: int | None
    description: str | None
    total_marks: int
    cut_score: float | None
    is_published: bool
    question_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class AssembleRequest(BaseModel):
    paper_number: int = Field(ge=1, le=4)
    title: str | None = None
    seed: int | None = None
    allow_partial: bool = False
    publish: bool = False


@router.get("/papers", response_model=list[PaperOut])
def list_papers(user: CurrentUser, db: DbSession) -> list[PaperOut]:
    stmt = select(ExamPaper).order_by(ExamPaper.id.desc())
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(ExamPaper.is_published.is_(True))
    papers = db.execute(stmt).scalars().all()
    out = []
    for paper in papers:
        item = PaperOut.model_validate(paper)
        item.question_count = len(paper.items)
        out.append(item)
    return out


@router.get("/papers/availability")
def paper_availability(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """What the bank can currently support, per paper."""
    counts = available_counts(db)
    seq_available = counts.get("SEQ", 0)
    vsaq_available = counts.get("VSAQ", 0)
    return {
        "approved_seq": seq_available,
        "approved_vsaq": vsaq_available,
        "papers": [
            {
                "paper_number": spec.number,
                "day": spec.day,
                "seq_required": spec.seq_count,
                "vsaq_required": spec.vsaq_count,
                "can_assemble": seq_available >= spec.seq_count
                and vsaq_available >= spec.vsaq_count,
                "writing_minutes": spec.writing_minutes,
                "total_marks": spec.total_marks,
            }
            for spec in PAPER_SPECS.values()
        ],
    }


@router.post("/papers/assemble", status_code=status.HTTP_201_CREATED)
def assemble(payload: AssembleRequest, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    spec = PAPER_SPECS[payload.paper_number]
    title = payload.title or f"Mock Paper {spec.number} (Day {spec.day})"
    try:
        paper, report = assemble_paper(
            db, spec, title, created_by_id=admin.id, seed=payload.seed,
            strict=not payload.allow_partial,
        )
    except AssemblyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.publish:
        paper.is_published = True
        db.commit()

    item = PaperOut.model_validate(paper)
    item.question_count = len(paper.items)
    return {
        "paper": item.model_dump(),
        "report": {
            "seq_selected": report.seq_selected,
            "vsaq_selected": report.vsaq_selected,
            "seq_required": report.seq_required,
            "vsaq_required": report.vsaq_required,
            "subspecialties": report.subspecialties,
            "shortfalls": report.shortfalls,
        },
    }


@router.post("/papers/{paper_id}/publish", response_model=PaperOut)
def publish_paper(
    paper_id: int, admin: AdminUser, db: DbSession, published: bool = True
) -> PaperOut:
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    paper.is_published = published
    db.commit()
    item = PaperOut.model_validate(paper)
    item.question_count = len(paper.items)
    return item


@router.delete("/papers/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_paper(paper_id: int, admin: AdminUser, db: DbSession) -> None:
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    sittings = db.execute(
        select(ExamSession.id).where(ExamSession.paper_id == paper_id)
    ).scalars().all()
    if sittings:
        raise HTTPException(
            status_code=409,
            detail=f"{len(sittings)} sitting(s) reference this paper. Unpublish it instead.",
        )
    db.delete(paper)
    db.commit()


# --- Sittings -------------------------------------------------------------
class StartSessionRequest(BaseModel):
    paper_id: int
    is_timed: bool = True


class SessionSummary(BaseModel):
    id: int
    paper_id: int
    paper_title: str
    paper_number: int | None
    phase: str
    is_timed: bool
    started_at: datetime | None
    submitted_at: datetime | None
    grading_status: str
    created_at: datetime


def _summary(db: DbSession, session: ExamSession) -> SessionSummary:
    paper = db.get(ExamPaper, session.paper_id)
    return SessionSummary(
        id=session.id,
        paper_id=session.paper_id,
        paper_title=paper.title if paper else "(deleted paper)",
        paper_number=paper.paper_number if paper else None,
        phase=session.phase,
        is_timed=session.is_timed,
        started_at=session.started_at,
        submitted_at=session.submitted_at,
        grading_status=session.grading_status,
        created_at=session.created_at,
    )


def _load_session(db: DbSession, session_id: int, user) -> ExamSession:
    return load_owned(db, ExamSession, session_id, user)


def _clock_for(db: DbSession, session: ExamSession):
    paper = db.get(ExamPaper, session.paper_id)
    return compute_clock(
        session.started_at,
        spec_for_paper(paper.paper_number if paper else None),
        submitted_at=session.submitted_at,
        is_timed=session.is_timed,
    )


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(user: CurrentUser, db: DbSession) -> list[SessionSummary]:
    stmt = select(ExamSession).order_by(ExamSession.id.desc())
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(ExamSession.user_id == user.id)
    return [_summary(db, s) for s in db.execute(stmt).scalars().all()]


@router.post("/sessions", response_model=SessionSummary, status_code=status.HTTP_201_CREATED)
def start_session(
    payload: StartSessionRequest, user: CurrentUser, db: DbSession
) -> SessionSummary:
    paper = db.get(ExamPaper, payload.paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not paper.is_published and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="This paper is not available")

    session = ExamSession(
        user_id=user.id,
        paper_id=paper.id,
        is_timed=payload.is_timed,
        phase=PHASE_NOT_STARTED,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _summary(db, session)


@router.post("/sessions/{session_id}/begin")
def begin_session(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Start the clock. Irreversible."""
    session = _load_session(db, session_id, user)
    if session.started_at is not None:
        raise HTTPException(status_code=400, detail="This sitting has already begun")
    session.started_at = datetime.now(timezone.utc)
    db.commit()
    clock = _clock_for(db, session)
    session.phase = clock.phase
    db.commit()
    return clock.as_dict()


@router.get("/sessions/{session_id}/clock")
def get_clock(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Authoritative time source. The client resyncs against this."""
    session = _load_session(db, session_id, user)
    clock = _clock_for(db, session)
    if session.phase != clock.phase:
        session.phase = clock.phase
        db.commit()
    return clock.as_dict()


@router.get("/sessions/{session_id}")
def get_session(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    session = _load_session(db, session_id, user)
    clock = _clock_for(db, session)
    paper = db.get(ExamPaper, session.paper_id)

    payload: dict[str, Any] = {
        "session": _summary(db, session).model_dump(),
        "clock": clock.as_dict(),
        "paper": {
            "id": paper.id,
            "title": paper.title,
            "paper_number": paper.paper_number,
            "day": paper.day,
            "total_marks": paper.total_marks,
            "description": paper.description,
        } if paper else None,
        "reading_notes": session.reading_notes or "",
    }

    if not clock.can_view_questions:
        # During preparation the paper must not be readable at all.
        payload["sections"] = None
        payload["locked_reason"] = (
            "The paper opens when the reading period begins."
        )
        return payload

    answers = {
        a.part_id: a.text
        for a in db.execute(
            select(Answer).where(Answer.session_id == session.id)
        ).scalars().all()
    }

    items = sorted(paper.items, key=lambda i: (i.section, i.position)) if paper else []

    # Opening a paper touches every question's parts and figures. Loaded lazily
    # that is three round trips per question; eagerly it is three in total.
    questions_by_id = {
        q.id: q
        for q in db.execute(
            select(Question)
            .where(Question.id.in_([i.question_id for i in items] or [0]))
            .options(selectinload(Question.parts), selectinload(Question.figures))
        ).scalars().all()
    }

    sections: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
    for item in items:
        question = questions_by_id.get(item.question_id)
        if question is None:
            continue
        sections.setdefault(item.section, []).append(
            _question_payload(db, question, answers, item.position)
        )

    payload["sections"] = sections
    return payload


def _question_payload(
    db: DbSession, question: Question, answers: dict[int, str], position: int
) -> dict[str, Any]:
    figures = [
        {
            "id": f.id,
            "label": f.label,
            "caption": f.caption,
            "image_id": f.image_id,
        }
        for f in sorted(question.figures, key=lambda f: f.position)
    ]
    return {
        "id": question.id,
        "position": position,
        "question_type": question.question_type,
        "subspecialty": question.subspecialty,
        "topic": question.topic,
        "stem": question.stem,
        "total_marks": question.total_marks,
        "figures": figures,
        # Model answers are deliberately absent until after submission.
        "parts": [
            {
                "id": part.id,
                "label": part.label,
                "text": part.text,
                "marks": part.marks,
                "preamble": part.preamble,
                "answer": answers.get(part.id, ""),
            }
            for part in sorted(question.parts, key=lambda p: p.position)
        ],
    }


class AnswerSave(BaseModel):
    part_id: int
    text: str


class SaveAnswersRequest(BaseModel):
    answers: list[AnswerSave]
    reading_notes: str | None = None


@router.put("/sessions/{session_id}/answers")
def save_answers(
    session_id: int, payload: SaveAnswersRequest, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Autosave endpoint. Called every few seconds during writing."""
    session = _load_session(db, session_id, user)
    if session.submitted_at is not None:
        raise HTTPException(status_code=409, detail="This sitting has been submitted")

    clock = _clock_for(db, session)

    # Notes are writable during reading as well as writing.
    if payload.reading_notes is not None and clock.can_take_notes:
        session.reading_notes = payload.reading_notes

    if payload.answers and not accepts_writes(clock):
        raise HTTPException(
            status_code=409,
            detail="Writing time has ended; answers can no longer be changed.",
        )

    valid_part_ids = _valid_part_ids(db, session)
    saved = 0
    for item in payload.answers:
        if item.part_id not in valid_part_ids:
            continue
        existing = db.execute(
            select(Answer)
            .where(Answer.session_id == session.id)
            .where(Answer.part_id == item.part_id)
        ).scalar_one_or_none()
        word_count = len(item.text.split())
        if existing is None:
            db.add(
                Answer(
                    session_id=session.id,
                    part_id=item.part_id,
                    text=item.text,
                    word_count=word_count,
                )
            )
        else:
            existing.text = item.text
            existing.word_count = word_count
        saved += 1

    db.commit()
    return {"saved": saved, "clock": clock.as_dict()}


def _valid_part_ids(db: DbSession, session: ExamSession) -> set[int]:
    """Part ids belonging to this sitting's paper.

    Guards against a client saving answers against arbitrary question parts.
    """
    question_ids = db.execute(
        select(ExamPaperQuestion.question_id).where(
            ExamPaperQuestion.paper_id == session.paper_id
        )
    ).scalars().all()
    if not question_ids:
        return set()
    parts: set[int] = set()
    for question_id in question_ids:
        question = db.get(Question, question_id)
        if question:
            parts.update(p.id for p in question.parts)
    return parts


@router.post("/sessions/{session_id}/submit")
def submit_session(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    session = _load_session(db, session_id, user)
    if session.submitted_at is not None:
        raise HTTPException(status_code=400, detail="Already submitted")
    if session.started_at is None:
        raise HTTPException(status_code=400, detail="This sitting has not begun")

    session.submitted_at = datetime.now(timezone.utc)
    session.phase = PHASE_SUBMITTED
    db.commit()

    answered = db.execute(
        select(Answer).where(Answer.session_id == session.id)
    ).scalars().all()

    job_id = None
    if SettingsStore(db).get_bool("exam.auto_grade_on_submit", True):
        session.grading_status = "queued"
        db.commit()
        job_id = create_job(
            db,
            JOB_GRADE_SESSION,
            payload={"session_id": session.id},
            created_by_id=user.id,
            message="Marking your paper",
        ).id

    return {
        "submitted_at": session.submitted_at.isoformat(),
        "answers_recorded": len([a for a in answered if a.text.strip()]),
        "grading_status": session.grading_status,
        "grading_job_id": job_id,
    }


@router.post("/sessions/{session_id}/grade", status_code=status.HTTP_202_ACCEPTED)
def grade_session(
    session_id: int, user: CurrentUser, db: DbSession, only_missing: bool = True
) -> dict[str, Any]:
    """Mark a submitted sitting.

    Defaults to only_missing so pressing "Re-mark" on a partly-marked paper
    completes it rather than paying to redo work that already succeeded. Pass
    only_missing=false to force a full re-mark.
    """
    session = _load_session(db, session_id, user)
    if session.submitted_at is None:
        raise HTTPException(status_code=400, detail="This sitting has not been submitted")

    session.grading_status = "queued"
    db.commit()
    job = create_job(
        db,
        JOB_GRADE_SESSION,
        payload={"session_id": session.id, "only_missing": only_missing},
        created_by_id=user.id,
        message="Marking paper",
    )
    return {"job_id": job.id}


@router.get("/sessions/{session_id}/result")
def get_result(session_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Marked result with a per-key-point breakdown for every sub-question."""
    session = _load_session(db, session_id, user)
    result = db.execute(
        select(SessionResult).where(SessionResult.session_id == session.id)
    ).scalar_one_or_none()

    paper = db.get(ExamPaper, session.paper_id)
    items = sorted(paper.items, key=lambda i: (i.section, i.position)) if paper else []
    flagged = set(result.flagged_parts or []) if result else set()

    # A full paper has around 60 sub-questions, and fetching each one's grades
    # and answer individually meant 120 queries to render one result page. Both
    # sets belong to this sitting alone, so read them once and index by part.
    grades_by_part: dict[int, list[Grade]] = {}
    for grade in db.execute(
        select(Grade)
        .where(Grade.session_id == session.id)
        .order_by(Grade.examiner_pass)
    ).scalars().all():
        grades_by_part.setdefault(grade.part_id, []).append(grade)

    answers_by_part = {
        a.part_id: a
        for a in db.execute(
            select(Answer).where(Answer.session_id == session.id)
        ).scalars().all()
    }

    # The result reveals the marking key for every sub-question, so parts and
    # their answer points are loaded up front rather than one part at a time.
    questions_by_id = {
        q.id: q
        for q in db.execute(
            select(Question)
            .where(Question.id.in_([i.question_id for i in items] or [0]))
            .options(selectinload(Question.parts).selectinload(QuestionPart.answer_points))
        ).scalars().all()
    }

    questions: list[dict[str, Any]] = []
    for item in items:
        question = questions_by_id.get(item.question_id)
        if question is None:
            continue
        parts_payload = []
        for part in sorted(question.parts, key=lambda p: p.position):
            grades = grades_by_part.get(part.id, [])
            answer = answers_by_part.get(part.id)

            awarded = (
                sum(g.awarded_marks for g in grades) / len(grades) if grades else None
            )
            parts_payload.append(
                {
                    "id": part.id,
                    "label": part.label,
                    "text": part.text,
                    "marks": part.marks,
                    "your_answer": answer.text if answer else "",
                    "awarded": round(awarded, 2) if awarded is not None else None,
                    "flagged": part.id in flagged,
                    "examiners": [
                        {
                            "pass": g.examiner_pass,
                            "awarded": g.awarded_marks,
                            "feedback": g.feedback,
                            "breakdown": g.breakdown,
                        }
                        for g in grades
                    ],
                    "model_answer": [
                        {
                            "id": p.id,
                            "text": p.text,
                            "marks": p.marks,
                            "is_critical": p.is_critical,
                            "from_examiner_feedback": p.from_examiner_feedback,
                        }
                        for p in sorted(part.answer_points, key=lambda p: p.position)
                    ],
                }
            )

        question_awarded = [p["awarded"] for p in parts_payload if p["awarded"] is not None]
        questions.append(
            {
                "id": question.id,
                "section": item.section,
                "question_type": question.question_type,
                "subspecialty": question.subspecialty,
                "topic": question.topic,
                "stem": question.stem,
                "total_marks": question.total_marks,
                "awarded": round(sum(question_awarded), 2) if question_awarded else None,
                "parts": parts_payload,
            }
        )

    return {
        "session": _summary(db, session).model_dump(),
        "grading_status": session.grading_status,
        "result": {
            "total_awarded": result.total_awarded,
            "total_available": result.total_available,
            "percentage": result.percentage,
            "cut_score": result.cut_score,
            "outcome": result.outcome,
            "subspecialty_breakdown": result.subspecialty_breakdown,
            "overall_feedback": result.overall_feedback,
            "flagged_parts": result.flagged_parts,
            "ungraded_parts": result.ungraded_parts,
        } if result else None,
        "questions": questions,
    }


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: int, user: CurrentUser, db: DbSession) -> None:
    session = _load_session(db, session_id, user)
    db.execute(SessionResult.__table__.delete().where(SessionResult.session_id == session.id))
    db.delete(session)
    db.commit()


@router.get("/papers/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: int, user: CurrentUser, db: DbSession) -> PaperOut:
    paper = db.get(ExamPaper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not paper.is_published and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=404, detail="Paper not found")
    item = PaperOut.model_validate(paper)
    item.question_count = len(paper.items)
    return item
