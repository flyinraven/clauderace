"""Act on what `sittability` finds, cheapest remedy first.

`station_faults` has been the single definition of "can this be answered" for
some time, and it was right about every failure this bank has had. It had one
consumer: an admin page that displayed the list. Nothing repaired anything.

So the same faults were found again and again - by the audit, by a checker
written to look for one of them, and in the end by a candidate meeting them in
a circuit - while the repairs that existed (binding, sourcing, reconciling) ran
on their own selection criteria, each of which disagreed with `station_faults`
in some particular. Sourcing selected on `image_wanted`, so the twelve
questions already showing a blank screen were the only ones it never went
looking for. That is not a bug in sourcing. It is the absence of anything whose
job was to close a fault.

This is that thing. For each faulty station it applies, in order:

  1. binding    - free, no model call, no search: give the question an
                  investigation the examiners' own report already contains.
  2. sourcing   - only faults marked `fixable_by_sourcing`, and only after
                  binding has failed to fix them. Spends search quota.
  3. reconcile  - last, because it is the only step that gives something up:
                  the question stops promising a picture and carries the
                  finding in words instead. Never run on a fault an image
                  could still have fixed.

The order is the point. Reconciling first buys words for a question whose
picture was sitting unclaimed in its own station, and every one of those is a
station made permanently worse to save a search that was never needed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import OsceStation
from app.services.ai import AIClient
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.sittability import station_faults

logger = logging.getLogger(__name__)

JOB_REPAIR_STATIONS = "repair_stations"

# Every fault kind `station_faults` can emit, and which step closes it. A kind
# missing from here fails the test that walks them, so a new fault cannot be
# added without someone deciding who repairs it - which is exactly how the
# existing kinds came to have no owner.
REMEDIES: dict[str, str] = {
    "no_opening_image": "source",
    "too_few_views": "source",
    "duplicate_image": "source",
    "low_confidence": "source",
    # A stand-in for the real appearance. Another search may find a faithful
    # one; if it does not, the stand-in stays, because a representative image
    # beats a blank screen.
    "representative_only": "source",
    "not_approved": "human",
    "presents_nothing": "reconcile",
    "impossible_request": "reconcile",
    "missing_investigation": "bind_then_source_then_reconcile",
    "wrong_eye": "bind_then_source",
    "answers_itself": "unleak",
    "missing_side": "source",
    "missing_structure": "source",
    # The question hands over a modality the station is not showing. Sourcing
    # gets one attempt at the thing it names, and where that finds nothing the
    # reconcile pass restates the question as what the candidate can actually
    # answer from - which is how "here is a slit lamp view of the right eye"
    # over a fundus photograph should have been closed, instead of reaching a
    # sitting and being found by hand.
    "promises_what_is_not_shown": "bind_then_source_then_reconcile",
    # Both need a person. A stem that gives away its own rubric is fixed by
    # rewording, and the automated rewording is a model call that fires on
    # enough well-formed questions to spend on stations already right; an
    # unmarked question is fixed by moving marks off a sibling, and inventing
    # them would change what the paper is out of. Both are in
    # NOT_WORTH_SPENDING_ALONE so neither pulls a station into a paid run.
    "stem_gives_away_rubric": "human",
    "unmarked_question": "human",
    # Not sourcing. Searching is what produced the wrong images in the first
    # place, and nothing about this fault makes the next search better than the
    # last one. Recorded so it is known; a person decides.
    "no_view_of_the_patient": "human",
}


def _source_from_hints(
    db: Session,
    client: AIClient,
    station: OsceStation,
    job_id: int | None,
    faults: list,
) -> int:
    """Search for exactly what `missing_side` and `missing_structure` name.

    Those faults exist because the rubric never named a second eye or a
    structure, so `station_views` has nothing for `source_coverage_images` to
    search from - the fault is right and the rubric-driven sourcing finds
    nothing anyway. Each fault already built its own search description as
    `sourcing_hint`; this asks for exactly that, one new figure per hint.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images.sourcing import source_image_for_station

    attached = 0
    seen: set[str] = set()
    for fault in faults:
        hint = fault.sourcing_hint
        if not hint or hint in seen:
            continue
        seen.add(hint)
        figure = OsceFigure(
            station_id=station.id,
            position=max((f.position for f in station.figures), default=-1) + 1,
            wanted_description=hint,
        )
        db.add(figure)
        db.flush()
        attached += int(bool(
            source_image_for_station(db, client, station, job_id, figure=figure)
            .get("attached")
        ))
    return attached


def _resource_bound_figures(
    db: Session,
    client: AIClient,
    station: OsceStation,
    job_id: int | None,
    faults: list,
) -> int:
    """Re-search a figure IN PLACE, for a fault about one a question already has.

    `_source_from_hints` always creates a new, unbound figure - right for
    missing_side and missing_structure, where nothing was ever bound. Wrong
    enough here to defeat the point: low_confidence and wrong_eye are about a
    figure a question's own `figure_id` already points at, so adding a
    different figure beside it leaves the question showing the same bad one.
    This updates that figure's own wanted_description and re-searches it,
    which is what `source_image_for_station(figure=...)` has always been for.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images.sourcing import source_image_for_station

    attached = 0
    seen: set[int] = set()
    for fault in faults:
        fid = fault.target_figure_id
        if not fid or fid in seen:
            continue
        seen.add(fid)
        figure = db.get(OsceFigure, fid)
        if figure is None:
            continue
        if fault.sourcing_hint:
            figure.wanted_description = fault.sourcing_hint
        attached += int(bool(
            source_image_for_station(db, client, station, job_id, figure=figure)
            .get("attached")
        ))
    return attached


def repair_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Close what can be closed on one station. Returns what each step did."""
    from app.services.osce.reconcile import reconcile_station, unleak_station
    from app.services.osce.station_images.ingested import (
        bind_ingested_figures_to_questions,
    )
    from app.services.osce.station_images.sourcing import (
        opening_figures,
        source_coverage_images,
        source_image_for_station,
        source_prompt_images,
    )

    done: dict[str, Any] = {"stations": 1}

    # First, and before the early return below. A station can have every image
    # it needs and still ask "give me three differential diagnoses for this
    # patient's presentation", which names nothing to differentiate. Those
    # stations have no image fault at all, so returning early on that basis
    # meant twelve of fourteen were never offered to the rewrite.
    done.update({
        k: v for k, v in unleak_station(db, client, station, job_id=job_id).items()
        if v
    })

    before = station_faults(station)
    if not before:
        done["already_sittable"] = 1
        return done

    # 1. Free. Always worth doing, and it can only add a picture.
    done["bound"] = bind_ingested_figures_to_questions(db, client, station).get("bound", 0)
    db.refresh(station)

    # 2. Paid, and only for what a search could actually fix. Re-reading the
    #    faults first means binding's work is not paid for a second time.
    remaining = station_faults(station)
    kinds = {f.kind for f in remaining if f.fixable_by_sourcing}
    if kinds:
        attached = 0
        try:
            # The patient first: a station with no view of the patient is worse
            # off than one missing an investigation, and it is also the image
            # every "examine and describe" mark depends on.
            if "no_opening_image" in kinds or not [
                f for f in opening_figures(station) if f.image_id
            ]:
                attached += int(bool(
                    source_image_for_station(db, client, station, job_id).get("attached")
                ))
            if "too_few_views" in kinds:
                attached += source_coverage_images(
                    db, client, station, job_id
                ).get("attached", 0)
            if kinds & {"missing_side", "missing_structure"}:
                attached += _source_from_hints(db, client, station, job_id, remaining)
            if kinds & {"low_confidence", "wrong_eye"}:
                attached += _resource_bound_figures(db, client, station, job_id, remaining)
            if "missing_investigation" in kinds:
                attached += source_prompt_images(
                    db, client, station, job_id
                ).get("attached", 0)
            done["sourced"] = attached
            db.refresh(station)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the run
            db.rollback()
            logger.warning("Could not source for station %s: %s", station.id, exc)
            done["source_failed"] = 1

    # 3. Last resort: give the question words. Anything still missing here has
    #    had both a free binding and a paid search, so nothing is given up that
    #    a picture could have kept.
    if station_faults(station):
        tally = reconcile_station(db, client, station, job_id=job_id)
        done.update({k: v for k, v in tally.items() if v})

    done["faults_before"] = len(before)
    done["faults_after"] = len(station_faults(station))
    return done


@register_handler(JOB_REPAIR_STATIONS)
def handle_repair_stations(ctx: JobContext) -> bool:
    """One station per chunk, so a slow search cannot stall the queue."""
    station_ids: list[int] = ctx.payload.get("station_ids") or []
    if not station_ids:
        raise JobHandlerError("No station_ids supplied")
    if not ctx.job.total_steps:
        ctx.set_total(len(station_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(station_ids) or ctx.cancelled:
        return True

    station = ctx.db.get(OsceStation, station_ids[index])
    if station is not None:
        try:
            outcome = repair_station(ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id)
            running = dict(ctx.job.result or {})
            for key, value in outcome.items():
                if isinstance(value, int):
                    running[key] = running.get(key, 0) + value
            ctx.set_result(**running)
        except Exception as exc:  # noqa: BLE001
            ctx.db.rollback()
            logger.exception("Could not repair station %s", station.id)
            running = dict(ctx.job.result or {})
            running["failed"] = running.get("failed", 0) + 1
            ctx.set_result(**running)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Repaired {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)


# Faults that do not stop a candidate answering, and so do not on their own
# justify a search. A representative image is a stand-in for the real
# appearance, not a blank screen, and re-sourcing all of them would have been
# 58 of the 78 searches in the first repair run for no station made answerable.
# They stay in `station_faults` - the audit should still say so - and a station
# being searched for some other reason may still be improved by the pass.
NOT_WORTH_SPENDING_ALONE = {
    "representative_only",
    "stem_gives_away_rubric",
    "unmarked_question",
    "no_view_of_the_patient",
}


def stations_needing_repair(db: Session, skip: set[int] | None = None) -> list[int]:
    """Every station a candidate could not fully answer today.

    `skip` is the stations already sat: their marks are recorded against the
    wording that was on screen at the time, and changing it now would make the
    feedback describe a station that never existed.
    """
    from sqlalchemy import select

    skip = skip or set()
    out = []
    for station in db.execute(select(OsceStation).order_by(OsceStation.id)).scalars():
        if station.id in skip or not (station.prompts or []):
            continue
        kinds = {f.kind for f in station_faults(station)}
        if kinds - NOT_WORTH_SPENDING_ALONE:
            out.append(station.id)
    return out
