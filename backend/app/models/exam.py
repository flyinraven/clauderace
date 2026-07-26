"""Exam papers, sittings, answers and grades."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.constants import PHASE_NOT_STARTED
from app.models.base import Base, TimestampMixin, UTCDateTime


class ExamPaper(TimestampMixin, Base):
    """An assembled paper: Paper 1-4 written, or an OSCE circuit."""

    __tablename__ = "exam_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    paper_type: Mapped[str] = mapped_column(String(20), default="written", nullable=False)
    day: Mapped[int | None] = mapped_column(Integer)
    paper_number: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)

    total_marks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Angoff cut score in marks, computed from member questions at assembly and
    # recomputed whenever membership or ratings change.
    cut_score: Mapped[float | None] = mapped_column(Float)

    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    items: Mapped[list["ExamPaperQuestion"]] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by="ExamPaperQuestion.position",
    )


class ExamPaperQuestion(Base):
    __tablename__ = "exam_paper_questions"
    __table_args__ = (
        UniqueConstraint("paper_id", "question_id", name="uq_paper_question"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # "A" for the SEQ section, "B" for the VSAQ section.
    section: Mapped[str] = mapped_column(String(5), default="A", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    paper: Mapped[ExamPaper] = relationship(back_populates="items")


class ExamSession(TimestampMixin, Base):
    """One candidate's sitting of one paper.

    The clock is derived entirely from `started_at` plus the paper spec, so it
    survives page refreshes, and cannot be extended by the client.
    """

    __tablename__ = "exam_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), index=True, nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    phase: Mapped[str] = mapped_column(String(20), default=PHASE_NOT_STARTED, nullable=False)

    # Untimed practice ignores the clock entirely.
    is_timed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Scratch pad written during the 15-minute reading phase.
    reading_notes: Mapped[str | None] = mapped_column(Text)

    grading_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)

    paper: Mapped[ExamPaper] = relationship()
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Answer(TimestampMixin, Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("session_id", "part_id", name="uq_answer_session_part"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("question_parts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    session: Mapped[ExamSession] = relationship(back_populates="answers")


class Grade(TimestampMixin, Base):
    """One examiner's marks for one answer.

    Two rows exist per part (examiner_pass 1 and 2), mirroring the real exam's
    two-examiner marking. The reported mark is their average.
    """

    __tablename__ = "grades"
    __table_args__ = (
        UniqueConstraint("session_id", "part_id", "examiner_pass", name="uq_grade_unique_pass"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    part_id: Mapped[int] = mapped_column(
        ForeignKey("question_parts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    examiner_pass: Mapped[int] = mapped_column(Integer, nullable=False)

    awarded_marks: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    available_marks: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Per-key-point detail: [{point_id, point_text, marks, awarded, comment}]
    breakdown: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    feedback: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(String(120))


class SessionResult(TimestampMixin, Base):
    """Final aggregated outcome for a sitting."""

    __tablename__ = "session_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    total_awarded: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_available: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    percentage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cut_score: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(20))

    # {subspecialty: {awarded, available, percentage}}
    subspecialty_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    overall_feedback: Mapped[str | None] = mapped_column(Text)
    flagged_parts: Mapped[list[int] | None] = mapped_column(JSON)

    # Sub-questions that could not be marked (usually a provider rate limit).
    # While this is non-empty the result carries no pass/fail verdict.
    ungraded_parts: Mapped[list[int] | None] = mapped_column(JSON)

    session: Mapped[ExamSession] = relationship()
