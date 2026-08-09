"""Re-caption figures that were captioned while being told what to expect.

Every figure attached before `describe_blind` existed carries a caption written
by the graded verification, which was given the station's signs and could agree
with them without having looked. A montage of one patient's unilateral Brown's
syndrome is captioned "bilateral" at confidence 1.00.

That was always untidy. It became load-bearing when questions started being
matched to their images by what the caption says: a caption echoing the request
hides exactly the mismatch that check exists to find.

This looks at each stored image once, with the station withheld, and replaces
the caption with what came back. It also fills `modality`, which is null on
every figure that never went through a search - 342 of them - leaving the
modality gate nothing to compare against.

Nothing is rejected and no image is detached. A figure whose blind description
disagrees with what was asked for is downgraded from faithful to
representative, with the disagreement written beside it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Image, OsceFigure, OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.station_images.constants import MIN_REPRESENTATIVE_CONFIDENCE
from app.services.osce.station_images.verify import (
    blind_disagreement,
    describe_blind,
    label_side,
)

logger = logging.getLogger(__name__)

JOB_RECAPTION_FIGURES = "recaption_station_figures"


def figures_needing_caption(db: Session) -> list[int]:
    """Every figure with an image, oldest first.

    All of them: a caption written by the graded pass cannot be told apart from
    one written blind by looking at it, which is the whole problem.
    """
    return list(
        db.execute(
            select(OsceFigure.id)
            .where(OsceFigure.image_id.is_not(None))
            .order_by(OsceFigure.id)
        ).scalars().all()
    )


def recaption_figure(db: Session, client: AIClient, figure_id: int) -> str:
    """Describe one stored image blind and keep what it says."""
    figure = db.get(OsceFigure, figure_id)
    if figure is None or figure.image_id is None:
        return "missing"
    image = db.get(Image, figure.image_id)
    if image is None or not image.data:
        return "missing"

    blind = describe_blind(client, image.data, image.content_type or "image/jpeg")
    caption = label_side(str(blind.get("caption") or ""), blind.get("side"))
    if not caption:
        return "no_caption"

    figure.caption = caption
    figure.modality = str(blind.get("modality") or "").strip() or figure.modality

    station = db.get(OsceStation, figure.station_id)
    outcome = "recaptioned"
    if station is not None:
        note = blind_disagreement(blind, figure.wanted_description, station)
        if note:
            existing = (figure.verification_notes or "").split("  [Looked at without")[0]
            figure.verification_notes = f"{existing}  [Looked at without the station: {note}]"
            if figure.verification_status == "faithful":
                figure.verification_status = "representative"
                figure.match_confidence = min(
                    figure.match_confidence or 1.0, MIN_REPRESENTATIVE_CONFIDENCE
                )
            outcome = "disagreed"
    db.commit()
    return outcome


@register_handler(JOB_RECAPTION_FIGURES)
def handle_recaption_figures(ctx: JobContext) -> bool:
    """One figure per chunk, so a dropped connection costs one call."""
    figure_ids: list[int] = ctx.payload.get("figure_ids") or []
    if not figure_ids:
        raise JobHandlerError("No figure_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(figure_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(figure_ids):
        return True
    if ctx.cancelled:
        return True

    try:
        outcome = recaption_figure(ctx.db, AIClient(ctx.db), figure_ids[index])
    except Exception as exc:  # noqa: BLE001 - one figure must not stop the sweep
        ctx.db.rollback()
        logger.exception("Could not re-caption figure %s", figure_ids[index])
        log_error(ctx.db, source="osce_recaption", message=str(exc),
                  context={"figure_id": figure_ids[index]})
        outcome = "failed"

    tally: dict[str, Any] = dict(ctx.job.result or {})
    tally[outcome] = tally.get(outcome, 0) + 1
    ctx.set_result(**tally)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Re-captioned {index + 1} of {len(figure_ids)} figures")
    return index + 1 >= len(figure_ids)
