"""Question bank: source documents, questions, model answers, figures, OSCE."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.constants import STATUS_DRAFT
from app.models.base import Base, TimestampMixin


class CurriculumStandard(TimestampMixin, Base):
    """A RANZCO curriculum performance standard, e.g. Glaucoma CL 5.2."""

    __tablename__ = "curriculum_standards"
    __table_args__ = (UniqueConstraint("subspecialty", "code", name="uq_curriculum_sub_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subspecialty: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    descriptor: Mapped[str] = mapped_column(Text, nullable=False)


class SourceDocument(TimestampMixin, Base):
    """An uploaded past paper / examiners' report awaiting or after ingestion."""

    __tablename__ = "source_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)

    # Free-text provenance, e.g. "2026 Semester 1" / "Written" | "OSCE".
    exam_period: Mapped[str | None] = mapped_column(String(60))
    document_kind: Mapped[str | None] = mapped_column(String(30))

    status: Mapped[str] = mapped_column(String(30), default="uploaded", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    questions: Mapped[list["Question"]] = relationship(back_populates="source_document")


class Image(TimestampMixin, Base):
    """A clinical image, either extracted from a PDF or fetched from the web.

    `source_url` records provenance for every web-sourced image so an admin can
    audit and remove anything that should not be there.
    """

    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(60), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    origin: Mapped[str] = mapped_column(String(30), default="pdf", nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_page_url: Mapped[str | None] = mapped_column(Text)
    attribution: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(String(120))
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    source_page_number: Mapped[int | None] = mapped_column(Integer)

    # AI-generated description of the clinical findings, used for grading and
    # for candidates using assistive technology.
    ai_description: Mapped[str | None] = mapped_column(Text)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Question(TimestampMixin, Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_type: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    subspecialty: Mapped[str | None] = mapped_column(String(60), index=True)
    topic: Mapped[str | None] = mapped_column(String(255))
    purpose: Mapped[str | None] = mapped_column(Text)
    stem: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Raw curriculum standard text as printed, plus parsed codes.
    curriculum_standard_raw: Mapped[str | None] = mapped_column(Text)
    curriculum_codes: Mapped[list[str] | None] = mapped_column(JSON)

    total_marks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    source: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    exam_period: Mapped[str | None] = mapped_column(String(60))
    original_number: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_DRAFT, index=True, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(String(20))

    # Fraction (0-1) of marks a borderline candidate would score. Drives the
    # Angoff cut score for any paper containing this question.
    angoff_expected: Mapped[float | None] = mapped_column(Float)
    angoff_rationale: Mapped[str | None] = mapped_column(Text)

    model_answer_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    generation_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    source_document: Mapped[SourceDocument | None] = relationship(back_populates="questions")
    parts: Mapped[list["QuestionPart"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="QuestionPart.position",
    )
    figures: Mapped[list["Figure"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="Figure.position",
    )
    examiner_feedback: Mapped[list["ExaminerFeedback"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


class QuestionPart(TimestampMixin, Base):
    """A lettered sub-question, e.g. "a) ... (5 marks)".

    A VSAQ has exactly one part worth 2 marks; an SEQ's parts sum to 20.
    """

    __tablename__ = "question_parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(10))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Additional stem text printed between parts (RANZCO papers often advance
    # the clinical scenario partway through a question).
    preamble: Mapped[str | None] = mapped_column(Text)

    question: Mapped[Question] = relationship(back_populates="parts")
    answer_points: Mapped[list["ModelAnswerPoint"]] = relationship(
        back_populates="part",
        cascade="all, delete-orphan",
        order_by="ModelAnswerPoint.position",
    )


class ModelAnswerPoint(TimestampMixin, Base):
    """One discrete, markable key point in the model answer for a part."""

    __tablename__ = "model_answer_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("question_parts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # Points drawn from examiner feedback ("candidates commonly missed X") are
    # flagged so the UI can highlight them and grading can weight them.
    is_critical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    from_examiner_feedback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    accepted_alternatives: Mapped[list[str] | None] = mapped_column(JSON)

    part: Mapped[QuestionPart] = relationship(back_populates="answer_points")


class ExaminerFeedback(TimestampMixin, Base):
    """Per-examiner commentary transcribed from the examiners' report.

    This is the highest-value content in the source PDFs: it states exactly
    what the cohort missed, and conditions model-answer generation.
    """

    __tablename__ = "examiner_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    examiner_number: Mapped[int | None] = mapped_column(Integer)
    common_mistakes: Mapped[list[str] | None] = mapped_column(JSON)
    cohort_impression: Mapped[list[str] | None] = mapped_column(JSON)

    question: Mapped[Question] = relationship(back_populates="examiner_feedback")


class Figure(TimestampMixin, Base):
    """An image attached to a question, e.g. "Figure 3: FFA at 1 minute"."""

    __tablename__ = "figures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    image_id: Mapped[int | None] = mapped_column(ForeignKey("images.id", ondelete="SET NULL"))
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_parts.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    label: Mapped[str | None] = mapped_column(String(40))
    caption: Mapped[str | None] = mapped_column(Text)

    # Retained when no image could be sourced, so an admin can attach one later.
    wanted_description: Mapped[str | None] = mapped_column(Text)

    question: Mapped[Question] = relationship(back_populates="figures")
    image: Mapped[Image | None] = relationship()


class OsceStation(TimestampMixin, Base):
    """One of the 18 nine-minute OSCE clinical stations."""

    __tablename__ = "osce_stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_number: Mapped[int | None] = mapped_column(Integer)
    subspecialty: Mapped[str | None] = mapped_column(String(60), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    case_summary: Mapped[str | None] = mapped_column(Text)
    aims: Mapped[list[str] | None] = mapped_column(JSON)
    patient_history: Mapped[str | None] = mapped_column(Text)

    # All a candidate sees before they start: "An elderly woman", "A young boy".
    # The full history names the diagnosis often enough that showing it hands
    # over the answer, so it is withheld until the result alongside the signs.
    patient_demographic: Mapped[str | None] = mapped_column(String(120))

    findings: Mapped[str | None] = mapped_column(Text)
    diagnosis: Mapped[str | None] = mapped_column(Text)

    # A real examiner states some findings outright (acuity, IOP, refraction)
    # but expects the candidate to elicit the clinical signs. Showing all of
    # `findings` up front hands over the answer to any "describe what you see"
    # prompt, so they are split and only the given half is shown during the
    # sitting.
    findings_given: Mapped[str | None] = mapped_column(Text)
    findings_elicited: Mapped[str | None] = mapped_column(Text)
    findings_split_status: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False
    )
    cohort_performance: Mapped[str | None] = mapped_column(Text)
    common_mistakes: Mapped[list[str] | None] = mapped_column(JSON)

    # Candidate-facing prompts plus the marking rubric.
    tasks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    rubric: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    total_marks: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    # Ordered examiner questions for the turn-taking sitting: the station asks
    # A, the candidate speaks, then B, and so on. Each prompt carries its own
    # slice of the rubric so a spoken answer is marked against exactly what
    # that question was asking, and the seconds sum to the 9-minute station.
    # [{"label": "A", "text": "...", "seconds": 90,
    #   "rubric": [{"text": "...", "marks": 2, "is_critical": false}]}]
    prompts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    prompts_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False)

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    exam_period: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_DRAFT, index=True, nullable=False)
    angoff_expected: Mapped[float | None] = mapped_column(Float)

    figures: Mapped[list["OsceFigure"]] = relationship(
        back_populates="station",
        cascade="all, delete-orphan",
        order_by="OsceFigure.position",
    )


class OsceFigure(TimestampMixin, Base):
    """A clinical image shown at an OSCE station.

    The real OSCE puts a live patient in front of the candidate, so a station
    without an image cannot test visual recognition at all. Web-sourced images
    are checked against the station's own findings by a vision model before
    they are allowed to be shown: an image that does not match would penalise
    a candidate for correctly describing what is actually in front of them.
    """

    __tablename__ = "osce_figures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(
        ForeignKey("osce_stations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    image_id: Mapped[int | None] = mapped_column(ForeignKey("images.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)

    # What the station needs to show, written before any image is sourced.
    wanted_description: Mapped[str | None] = mapped_column(Text)
    search_query: Mapped[str | None] = mapped_column(Text)

    # Vision check: "pending" | "verified" | "rejected" | "unverified"
    verification_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )
    verification_notes: Mapped[str | None] = mapped_column(Text)
    # 0-1 confidence that the image genuinely shows the station's findings.
    match_confidence: Mapped[float | None] = mapped_column(Float)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Source URLs the user has already rejected for this station. A replacement
    # search skips them, so asking for another image never returns the same one.
    rejected_urls: Mapped[list[str] | None] = mapped_column(JSON)
    rejection_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    station: Mapped[OsceStation] = relationship(back_populates="figures")
    image: Mapped[Image | None] = relationship()
