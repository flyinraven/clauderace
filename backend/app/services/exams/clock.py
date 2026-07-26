"""Server-authoritative exam clock.

Every timing decision is derived from `ExamSession.started_at` plus the paper's
`PaperSpec`. Nothing about the schedule is stored per session and nothing is
taken from the client, so a candidate cannot gain time by refreshing, changing
their system clock, or replaying a request - and a Render cold start mid-paper
costs them only the seconds the request actually took.

Phase rules mirror the real RACE sitting:

  preparation  desktop check; the paper is not visible
  reading      the paper is visible, answer boxes are locked, notes allowed
  writing      answers may be edited
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.constants import (
    PAPER_SPECS,
    PHASE_EXPIRED,
    PHASE_NOT_STARTED,
    PHASE_PREP,
    PHASE_READING,
    PHASE_SUBMITTED,
    PHASE_WRITING,
    SUBMISSION_GRACE_SECONDS,
    PaperSpec,
)


@dataclass(frozen=True)
class ClockState:
    phase: str
    now: datetime
    started_at: datetime | None
    phase_ends_at: datetime | None
    paper_ends_at: datetime | None
    seconds_remaining_in_phase: int
    seconds_remaining_total: int
    can_view_questions: bool
    can_write_answers: bool
    can_take_notes: bool

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "server_time": self.now.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "phase_ends_at": self.phase_ends_at.isoformat() if self.phase_ends_at else None,
            "paper_ends_at": self.paper_ends_at.isoformat() if self.paper_ends_at else None,
            "seconds_remaining_in_phase": self.seconds_remaining_in_phase,
            "seconds_remaining_total": self.seconds_remaining_total,
            "can_view_questions": self.can_view_questions,
            "can_write_answers": self.can_write_answers,
            "can_take_notes": self.can_take_notes,
        }


def spec_for_paper(paper_number: int | None) -> PaperSpec:
    """Resolve the timing spec, defaulting to Paper 1's shape."""
    return PAPER_SPECS.get(paper_number or 1, PAPER_SPECS[1])


def compute_clock(
    started_at: datetime | None,
    spec: PaperSpec,
    *,
    submitted_at: datetime | None = None,
    is_timed: bool = True,
    now: datetime | None = None,
) -> ClockState:
    now = now or datetime.now(timezone.utc)

    if submitted_at is not None:
        return ClockState(
            phase=PHASE_SUBMITTED, now=now, started_at=started_at,
            phase_ends_at=None, paper_ends_at=None,
            seconds_remaining_in_phase=0, seconds_remaining_total=0,
            can_view_questions=True, can_write_answers=False, can_take_notes=False,
        )

    if started_at is None:
        return ClockState(
            phase=PHASE_NOT_STARTED, now=now, started_at=None,
            phase_ends_at=None, paper_ends_at=None,
            seconds_remaining_in_phase=0, seconds_remaining_total=0,
            can_view_questions=False, can_write_answers=False, can_take_notes=False,
        )

    # Untimed practice: everything unlocked, clock never runs out.
    if not is_timed:
        return ClockState(
            phase=PHASE_WRITING, now=now, started_at=started_at,
            phase_ends_at=None, paper_ends_at=None,
            seconds_remaining_in_phase=0, seconds_remaining_total=0,
            can_view_questions=True, can_write_answers=True, can_take_notes=True,
        )

    prep_ends = started_at + timedelta(minutes=spec.prep_minutes)
    reading_ends = prep_ends + timedelta(minutes=spec.reading_minutes)
    writing_ends = reading_ends + timedelta(minutes=spec.writing_minutes)

    if now < prep_ends:
        phase, phase_ends = PHASE_PREP, prep_ends
        view, write, notes = False, False, False
    elif now < reading_ends:
        phase, phase_ends = PHASE_READING, reading_ends
        view, write, notes = True, False, True
    elif now < writing_ends:
        phase, phase_ends = PHASE_WRITING, writing_ends
        view, write, notes = True, True, True
    else:
        return ClockState(
            phase=PHASE_EXPIRED, now=now, started_at=started_at,
            phase_ends_at=writing_ends, paper_ends_at=writing_ends,
            seconds_remaining_in_phase=0, seconds_remaining_total=0,
            can_view_questions=True, can_write_answers=False, can_take_notes=False,
        )

    return ClockState(
        phase=phase, now=now, started_at=started_at,
        phase_ends_at=phase_ends, paper_ends_at=writing_ends,
        seconds_remaining_in_phase=max(0, int((phase_ends - now).total_seconds())),
        seconds_remaining_total=max(0, int((writing_ends - now).total_seconds())),
        can_view_questions=view, can_write_answers=write, can_take_notes=notes,
    )


def accepts_writes(clock: ClockState, now: datetime | None = None) -> bool:
    """Whether an answer save should be honoured.

    A short grace window past the writing deadline absorbs network latency and
    a Render cold start on the final autosave, so a candidate does not lose
    their last edit to a request that was in flight when time expired.
    """
    if clock.can_write_answers:
        return True
    if clock.phase != PHASE_EXPIRED or clock.paper_ends_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    overrun = (now - clock.paper_ends_at).total_seconds()
    return 0 <= overrun <= SUBMISSION_GRACE_SECONDS
