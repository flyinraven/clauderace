"""Shared by more than one of the OSCE route modules."""

from __future__ import annotations

from typing import Any
from app.api.deps import DbSession, load_owned
from app.models import OsceSession
from app.services.imagesearch.relevance import is_investigation
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


def visible_figure(figure) -> dict[str, Any] | None:
    """One figure as the candidate may see it, or None if they may not.

    Two ways a figure reaches them, and a figure with neither reaches them not
    at all - a search that found nothing leaves a row behind, and showing it
    would put an empty frame on the screen.

      - an image that was attached and approved;
      - the protocol's last resort: no image could be found, so the examiner
        states the findings and the candidate reads those instead.

    A rejected figure is neither, so it is never returned. That is the point of
    rejecting it.

    Shared by the sitting and the review deliberately. They showed the same
    thing by two copies of this rule until the review needed images too, and a
    review that showed a figure the sitting withheld would be marking the
    candidate against something they never saw.
    """
    shows_image = bool(figure.image_id and figure.is_approved)
    described = (
        figure.described_findings if figure.described_findings_approved else None
    )
    if not shows_image and not described:
        return None
    return {
        "id": figure.id,
        "image_id": figure.image_id if shows_image else None,
        "caption": figure.caption if shows_image else None,
        "described_findings": described,
        "position": figure.position,
    }


def figures_for_prompt(by_id: dict[int, Any], prompt: dict[str, Any]) -> list[dict[str, Any]]:
    """The investigations one question asks the candidate to read."""
    out = []
    for figure_id in _bound_figure_ids(prompt):
        figure = by_id.get(figure_id)
        if figure is None:
            continue
        payload = visible_figure(figure)
        if payload:
            out.append(payload)
    return out


def opening_figures_payload(station) -> list[dict[str, Any]]:
    """What is on screen from the start: the patient, and nothing more.

    An image belonging to a question is not shown with the patient - an MRI on
    screen from the beginning answers the question before it is asked.

    Nor is an investigation that no question claimed. At a real station the
    candidate sees the patient and asks for the rest: "How would you confirm
    the diagnosis?" earns the Pentacam. Showing the printouts from the start
    inverts that - it hands over the answer and buries the view the rubric was
    actually written for. Station 155 opened on four corneal topography maps
    and one slit lamp photograph captioned "of one eye", and its eight marks
    for the graft and the apical scar could not be earned from any of them.

    A station whose every image is an investigation would be left with a blank
    screen, and blank is worse than early: those keep what they have, and the
    audit reports them as having no view of the patient.
    """
    owned = {i for p in (station.prompts or []) for i in _bound_figure_ids(p)}
    shown, held_back = [], []
    for figure in sorted(station.figures, key=lambda f: f.position):
        if figure.id in owned:
            continue
        payload = visible_figure(figure)
        if not payload:
            continue
        # Words are the examiner speaking, never an investigation.
        if figure.image_id and is_investigation(figure.modality):
            held_back.append(payload)
        else:
            shown.append(payload)
    return shown or held_back
