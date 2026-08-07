"""Shared by more than one of the OSCE route modules."""

from __future__ import annotations

from typing import Any
from app.api.deps import DbSession, load_owned
from app.models import OsceSession
from app.services.osce.circuit import compute_station_clock


# A single spoken answer. Generous enough for a rambling 3-minute reply,
# tight enough to reject a runaway recording.
MAX_AUDIO_BYTES = 15 * 1024 * 1024
ACCEPTED_AUDIO_PREFIXES = ("audio/", "video/mp4", "video/webm")


def _all_bound_ids(prompts: list[dict[str, Any]]) -> set[int]:
    """Every figure that travels with a question rather than with the patient."""
    return {i for p in prompts for i in _bound_figure_ids(p)}


def _bound_figure_ids(prompt: dict[str, Any]) -> list[int]:
    """The figures this question carries, in order, first one first.

    A question asking for two investigations holds `figure_ids`; one asking for
    a single image holds only `figure_id`, which is every station written before
    the list existed. `figure_id` is kept as the first of the list, so reading
    both and de-duplicating is what covers all of them.
    """
    ids = [i for i in (prompt.get("figure_ids") or []) if i]
    first = prompt.get("figure_id")
    if first and first not in ids:
        ids.insert(0, first)
    return ids


def _load_sitting(db: DbSession, session_id: int, user) -> OsceSession:
    return load_owned(db, OsceSession, session_id, user)


def _clock(sitting: OsceSession):
    return compute_station_clock(
        sitting.started_at, sitting.submitted_at, sitting.is_timed
    )
