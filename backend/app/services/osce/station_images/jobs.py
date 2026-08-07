"""The chunked job that sources a bank of stations, one station per chunk."""

from __future__ import annotations

import logging

from app.models import OsceStation
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.imagesearch.base import ImageSearchError
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.station_images.constants import (
    JOB_DESCRIBE_STATION_FIGURES,
    JOB_SOURCE_STATION_IMAGES,
)
from app.services.osce.station_images.describe import (
    _queue_settle,
    figures_needing_description,
)
from app.services.osce.station_images.sourcing import (
    opening_image_is_settled,
    source_coverage_images,
    source_image_for_station,
    source_prompt_images,
)

logger = logging.getLogger(__name__)


@register_handler(JOB_SOURCE_STATION_IMAGES)
def handle_source_station_images(ctx: JobContext) -> bool:
    """One station per chunk: search, verify, attach."""
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
            # Sourcing the whole bank is dozens of stations that only want an
            # ancillary image; `only_missing` keeps the spend on those.
            if ctx.payload.get("only_missing") and opening_image_is_settled(station):
                kept = list((ctx.job.result or {}).get("kept", []))
                kept.append(station.id)
                ctx.set_result(kept=kept)
            else:
                outcome = source_image_for_station(ctx.db, client, station, job_id=ctx.job.id)
                key = "attached" if outcome.get("attached") else "no_image"
                done = list((ctx.job.result or {}).get(key, []))
                done.append(station.id)
                ctx.set_result(**{key: done})

            if ctx.cancelled:
                return True

            # One photograph cannot carry a rubric that marks both eyes, so
            # the remaining views are filled before the station is called done.
            coverage = source_coverage_images(ctx.db, client, station, job_id=ctx.job.id)
            if coverage["attached"] or coverage["failed"]:
                tally = dict((ctx.job.result or {}).get("coverage", {}))
                tally["attached"] = tally.get("attached", 0) + coverage["attached"]
                tally["failed"] = tally.get("failed", 0) + coverage["failed"]
                ctx.set_result(coverage=tally)

            if ctx.cancelled:
                return True

            # The questions may each need an image of their own on top of the
            # one the candidate opens on.
            for_prompts = source_prompt_images(ctx.db, client, station, job_id=ctx.job.id)
            if any(for_prompts.values()):
                tally = dict((ctx.job.result or {}).get("prompt_images", {}))
                for key in ("attached", "failed", "impossible"):
                    tally[key] = tally.get(key, 0) + for_prompts.get(key, 0)
                ctx.set_result(prompt_images=tally)
        except ImageSearchError as exc:
            # Quota or credentials: every remaining station would fail too.
            ctx.db.rollback()
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})
            raise JobHandlerError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            ctx.db.rollback()
            logger.exception("Image sourcing failed for station %s", station.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(station.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Images: {index + 1} of {len(station_ids)}")

    finished = index + 1 >= len(station_ids)
    if finished:
        # Whatever searching could not fill now falls to the protocol's last
        # resort, without waiting to be asked. Queued here rather than beside
        # this job at ingest, because which figures still need words is only
        # known once every search has been tried.
        _queue_description_of_gaps(ctx)
        # Once, for the whole batch, and only once every search has been tried:
        # what a question really has on screen is not final until then. Per
        # station it would queue twenty-eight jobs for a twenty-eight station
        # run, and each would be reading a bank still being changed.
        _queue_reconcile(ctx, station_ids)
    return finished


def _queue_description_of_gaps(ctx: JobContext) -> None:
    from app.services.jobs.runner import create_job

    ids = figures_needing_description(ctx.db)
    if not ids:
        # Nothing to describe still has to be settled: the pass that follows is
        # what holds the end state, not a tidy-up after a description.
        _queue_settle(ctx, ctx.payload.get("station_ids") or [])
        return
    job = create_job(
        ctx.db,
        JOB_DESCRIBE_STATION_FIGURES,
        payload={"figure_ids": ids},
        created_by_id=ctx.job.created_by_id,
        total_steps=len(ids),
        message=f"Stating the findings for {len(ids)} view(s) with no image",
    )
    logger.info("Queued description job %s for %d figure(s)", job.id, len(ids))


def _queue_reconcile(ctx: JobContext, station_ids: list[int]) -> None:
    """Check the questions against what sourcing actually managed to attach.

    Here rather than at the point the questions were written, which is the only
    other place it could go and is too early: that moment knows what a question
    asked for, not what arrived. A station is reconciled once its own chunk has
    finished buying images, so the answer is final by the time it is read.
    """
    from app.services.jobs.runner import create_job
    from app.services.osce.reconcile import JOB_RECONCILE_QUESTIONS

    create_job(
        ctx.db,
        JOB_RECONCILE_QUESTIONS,
        payload={"station_ids": station_ids},
        total_steps=len(station_ids),
        message="Matching questions to the images that arrived",
    )
