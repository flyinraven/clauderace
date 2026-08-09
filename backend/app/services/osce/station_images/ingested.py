"""Figures that arrived inside a report rather than off the web.

They still have to pass the same gate, and they still have to be bound to the
question they belong to.
"""

from __future__ import annotations

import logging

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from app.models import Image, OsceFigure, OsceStation
from app.services.ai import AIClient
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.imagesearch.relevance import named_modality
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.station_images.constants import (
    FROM_PAPER,
    JOB_BIND_STATION_FIGURES,
    JOB_VERIFY_STATION_FIGURES,
    REVIEWABLE_STATUSES,
)
from app.services.osce.station_images.verify import label_side, leaked_term, verify_image

logger = logging.getLogger(__name__)


def verify_ingested_figures(
    db: Session, client: AIClient, station: OsceStation
) -> dict[str, Any]:
    """Record what each of the report's own figures is. Do not judge whether to show it.

    This used to be a gate, and everything about that was wrong for a paper's
    own images. The grader it called is written to screen web search results,
    where an annotation means somebody has labelled the abnormality for the
    candidate and a mismatched modality means the wrong picture was bought. Run
    against the examiners' report it rejects the report for looking like a
    report: a Humphrey printout is text and numbers by nature, an OCT report
    carries measurement overlays, and 118 real CTs, fields, OCTs and fundus
    photographs were dropped in one pass on that basis.

    An image printed in the examiners' report is an image the real candidates
    were shown. It goes live. The risk of showing a mark-distribution chart
    beside a station is small and visible; the risk of hiding the station's own
    photograph and buying a stranger's is neither.

    What the vision model is still good for is saying WHAT the image is, and
    that is now all it is asked: the modality is recorded so a question wanting
    an OCT can be handed the OCT this paper already contains, and a neutral
    caption replaces one that names the diagnosis. A figure whose check fails
    is shown anyway - the check is an improvement to it, not a condition of it.
    """
    kept, described, skipped = 0, 0, 0

    for figure in sorted(station.figures, key=lambda f: f.position):
        if figure.image_id is None or figure.verification_status not in REVIEWABLE_STATUSES:
            skipped += 1
            continue
        image = db.get(Image, figure.image_id)
        if image is None or image.origin != "pdf":
            skipped += 1
            continue

        # Live first, so that a failure below cannot leave it hidden.
        figure.verification_status = FROM_PAPER
        figure.is_approved = True
        kept += 1

        # The paper's own caption often names the diagnosis - "Figure 3: disc
        # in advanced glaucoma" - which answers the question before it is
        # asked. The station's leak guard applies to it exactly as it does to
        # findings stated in words.
        if figure.caption and leaked_term(figure.caption, station):
            figure.caption = None

        try:
            verdict = verify_image(db, client, station, image.data, image.content_type)
        except Exception as exc:  # noqa: BLE001 - one figure must not stop the rest
            logger.warning("Could not classify figure %s: %s", figure.id, exc)
            db.commit()
            continue

        figure.modality = str(verdict.get("modality") or "").strip().lower() or None
        figure.match_confidence = as_float(verdict.get("confidence"), 0.0)
        figure.verification_notes = str(verdict.get("shows") or "").strip() or None
        if not figure.caption:
            figure.caption = str(verdict.get("caption") or "").strip() or None
        # The graded pass has no notion of which eye it is looking at, so its
        # caption reads "of one eye" - the wording that cost station 155 eight
        # marks. It cannot name the side, but it must not pretend to: the empty
        # phrase comes out, and the re-captioning pass fills the side in when it
        # looks at the image blind.
        figure.caption = label_side(figure.caption, None)
        described += 1
        db.commit()

    db.commit()
    # "rejected" stays in the tally at zero: the job handler sums these keys
    # across stations and a missing one would read as a station that was never
    # looked at.
    return {"kept": kept, "rejected": 0, "described": described, "skipped": skipped}


def bind_ingested_figures_to_questions(
    db: Session, client: AIClient, station: OsceStation
) -> dict[str, Any]:
    """Give a question the report's own investigation, rather than buying one.

    Grading every ingested figure against the station's opening task was too
    blunt. "Please examine the patient's eye movements" expects an external
    photograph, so the four MRIs printed with that station were all marked the
    wrong modality and dropped - while question C of the same station asks the
    candidate to read an MRI of the brain, and the sourcing run was about to
    search the web for one.

    A figure nothing has claimed is therefore offered to each unanswered
    question whose investigation it names. The match must be exact: a request
    naming no modality of its own is skipped rather than guessed at, and a
    Pentacam is not accepted for a question that asked for a UBM. The vision
    grader then judges it against that question's own wording, so binding never
    rests on the caption alone.
    """
    prompts = list(station.prompts or [])
    claimed = {i for p in prompts for i in bound_figure_ids(p)}
    spare = [
        f for f in sorted(station.figures, key=lambda f: f.position)
        if f.image_id and f.id not in claimed
    ]
    if not spare:
        return {"bound": 0}

    bound = 0
    for prompt in prompts:
        wanted = str(prompt.get("image_wanted") or "").strip()
        if not wanted or bound_figure_ids(prompt) or prompt.get("image_impossible"):
            continue
        asked = named_modality(wanted)
        if asked is None:
            continue

        for figure in list(spare):
            image = db.get(Image, figure.image_id)
            if image is None or image.origin != "pdf":
                continue
            # What the classification pass recorded, falling back to the words
            # on the figure for rows written before it ran. Matching on the
            # caption alone was guesswork: a caption that happened not to name
            # its modality left the question unanswered and sent the station
            # off to buy the investigation the paper had already printed.
            mine = figure.modality or named_modality(
                f"{figure.caption or ''} {figure.verification_notes or ''}"
            )
            if mine != asked:
                continue

            figure.wanted_description = wanted
            figure.is_approved = True
            prompt["figure_id"] = figure.id
            spare.remove(figure)
            bound += 1
            break

    if bound:
        station.prompts = [dict(p) for p in prompts]
        flag_modified(station, "prompts")
        db.commit()
    return {"bound": bound}


def bound_figure_ids(prompt: dict[str, Any]) -> list[int]:
    ids = [i for i in (prompt.get("figure_ids") or []) if i]
    first = prompt.get("figure_id")
    if first and first not in ids:
        ids.insert(0, first)
    return ids


@register_handler(JOB_VERIFY_STATION_FIGURES)
def handle_verify_station_figures(ctx: JobContext) -> bool:
    """One station per chunk: grade the figures ingest took on trust."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            client = AIClient(ctx.db)
            outcome = verify_ingested_figures(ctx.db, client, station)
            # A figure the opening task had no use for may be exactly what a
            # later question asks the candidate to read.
            outcome.update(bind_ingested_figures_to_questions(ctx.db, client, station))
            tally = dict((ctx.job.result or {}).get("figures", {}))
            for key in ("kept", "rejected", "skipped", "bound"):
                tally[key] = tally.get(key, 0) + outcome.get(key, 0)
            ctx.set_result(figures=tally)
        except Exception as exc:  # noqa: BLE001
            ctx.db.rollback()
            logger.exception("Could not verify the figures of station %s", station.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Figures: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


def stations_with_bindable_figures(db: Session) -> list[int]:
    """Stations holding a paper figure nothing has claimed, and a question wanting one.

    Binding only ever ran inside the figure recheck, which selects stations by
    whether a figure still needs *verifying*. Once every figure had been
    verified that query returned nothing, so the binder became unreachable -
    with seventeen questions holding a restored request and the paper's own
    figures sitting unclaimed beside them.

    This asks the question the binder actually answers: is there anything here
    to match? It costs nothing to run. The binder makes no model calls; it
    compares the modality a question named against the modality a figure was
    recorded as, and refuses anything short of an exact match.
    """
    out: list[int] = []
    rows = db.execute(
        select(OsceStation.id, OsceStation.prompts).order_by(OsceStation.id)
    ).all()
    figures_by_station: dict[int, list[int]] = {}
    for figure_id, station_id in db.execute(
        select(OsceFigure.id, OsceFigure.station_id)
        .join(Image, Image.id == OsceFigure.image_id)
        .where(Image.origin == "pdf")
    ).all():
        figures_by_station.setdefault(station_id, []).append(figure_id)

    for station_id, prompts in rows:
        prompts = prompts or []
        claimed = {i for p in prompts for i in bound_figure_ids(p)}
        spare = [f for f in figures_by_station.get(station_id, []) if f not in claimed]
        if not spare:
            continue
        wants = any(
            str(p.get("image_wanted") or "").strip()
            and not bound_figure_ids(p)
            and not p.get("image_impossible")
            for p in prompts
        )
        if wants:
            out.append(station_id)
    return out


@register_handler(JOB_BIND_STATION_FIGURES)
def handle_bind_station_figures(ctx: JobContext) -> bool:
    """One station per chunk. No model calls: this is matching, not judging."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids):
        return True
    if ctx.cancelled:
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            outcome = bind_ingested_figures_to_questions(ctx.db, AIClient(ctx.db), station)
            running = dict(ctx.job.result or {})
            running["bound"] = running.get("bound", 0) + outcome.get("bound", 0)
            ctx.set_result(**running)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the run
            ctx.db.rollback()
            logger.exception("Could not bind the figures of station %s", station.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Bound {index + 1} of {len(station_ids)} stations")
    return index + 1 >= len(station_ids)


def stations_with_unchecked_figures(db: Session) -> list[int]:
    """Stations still showing images no vision model has ever looked at."""
    ids = db.execute(
        select(OsceFigure.station_id)
        .join(Image, Image.id == OsceFigure.image_id)
        .where(Image.origin == "pdf")
        .where(
            OsceFigure.verification_status.is_(None)
            | OsceFigure.verification_status.in_(
                [s for s in REVIEWABLE_STATUSES if s is not None]
            )
        )
        .distinct()
    ).scalars().all()
    return sorted(set(ids))
