"""OSCE circuits, station sittings, spoken responses and marks.

Kept separate from the written-exam tables because the unit of work differs:
a written paper is answered in text against sub-questions, whereas an OSCE
station is a timed spoken dialogue against an ordered list of examiner prompts.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
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

from app.models.base import Base, TimestampMixin, UTCDateTime


class AudioClip(TimestampMixin, Base):
    """A recorded spoken answer.

    Audio is deleted once it has been transcribed unless retention is switched
    on: a nine-station circuit produces around 45 clips a day, which would fill
    the hosting quota within weeks for no benefit once the transcript exists.
    """

    __tablename__ = "audio_clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # iOS Safari's MediaRecorder only emits audio/mp4 (AAC); Chromium emits
    # audio/webm (Opus). Both are stored as-is and declared to the transcriber.
    content_type: Mapped[str] = mapped_column(String(60), nullable=False)
    data: Mapped[bytes | None] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    is_discarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class OsceCircuit(TimestampMixin, Base):
    """A day's practice: nine stations, one per subspecialty."""

    __tablename__ = "osce_circuits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_for: Mapped[date | None] = mapped_column(Date, index=True)
    station_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)

    sittings: Mapped[list["OsceSession"]] = relationship(
        back_populates="circuit", cascade="all, delete-orphan"
    )


class OsceSession(TimestampMixin, Base):
    """One candidate's sitting of one station."""

    __tablename__ = "osce_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    station_id: Mapped[int] = mapped_column(
        ForeignKey("osce_stations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    circuit_id: Mapped[int | None] = mapped_column(
        ForeignKey("osce_circuits.id", ondelete="SET NULL"), index=True
    )

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_timed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Which prompt the candidate is on. The clock is still derived from
    # started_at, so this cannot be used to buy extra time.
    current_prompt_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    grading_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)

    circuit: Mapped[OsceCircuit | None] = relationship(back_populates="sittings")
    responses: Mapped[list["OsceResponse"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class OsceResponse(TimestampMixin, Base):
    """A spoken answer to one examiner prompt."""

    __tablename__ = "osce_responses"
    __table_args__ = (
        UniqueConstraint("session_id", "prompt_label", name="uq_osce_response_prompt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("osce_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    prompt_label: Mapped[str] = mapped_column(String(10), nullable=False)
    prompt_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    audio_clip_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_clips.id", ondelete="SET NULL")
    )
    # What the transcriber heard.
    transcript: Mapped[str | None] = mapped_column(Text)
    # What the candidate corrected it to, if anything. Marking uses this when
    # present, so a mis-heard term does not cost marks that were earned.
    transcript_edited: Mapped[str | None] = mapped_column(Text)

    transcription_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )
    transcription_error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    session: Mapped[OsceSession] = relationship(back_populates="responses")

    @property
    def marking_text(self) -> str:
        return (self.transcript_edited or self.transcript or "").strip()


class OsceGrade(TimestampMixin, Base):
    """One examiner's marks for one prompt's spoken answer."""

    __tablename__ = "osce_grades"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "prompt_label", "examiner_pass", name="uq_osce_grade_pass"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("osce_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    prompt_label: Mapped[str] = mapped_column(String(10), nullable=False)
    examiner_pass: Mapped[int] = mapped_column(Integer, nullable=False)

    awarded_marks: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    available_marks: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    breakdown: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    feedback: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(String(120))


class OsceResult(TimestampMixin, Base):
    """Aggregated outcome for one station sitting."""

    __tablename__ = "osce_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("osce_sessions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    total_awarded: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_available: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    percentage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cut_score: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(20))
    overall_feedback: Mapped[str | None] = mapped_column(Text)
    flagged_prompts: Mapped[list[str] | None] = mapped_column(JSON)
    ungraded_prompts: Mapped[list[str] | None] = mapped_column(JSON)

    session: Mapped[OsceSession] = relationship()
