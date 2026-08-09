"""Deciding a station has the images it is going to get.

Without this a re-source keeps paying to search for pictures that do not exist.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from app.models import OsceStation
from app.services.errors import log_error
from app.services.imagesearch.relevance import named_modality, unsourceable_reason
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.station_images.constants import JOB_SETTLE_STATIONS
from app.services.osce.sittability import answers_a_view
from app.services.osce.station_images.verify import leaked_term, verbatim_findings_floor
from app.services.osce.station_images.ingested import bound_figure_ids

logger = logging.getLogger(__name__)


def settle_station(db: Session, station: OsceStation) -> dict[str, int]:
    """Make the station's figures match the protocol, whatever wrote them.

    Every rule in this module applies when a figure is written, so a rule that
    changes leaves every station built under the old one exactly as it was.
    That is why the same complaint kept coming back after each fix: the fix was
    real and the stations in front of the user were untouched by it.

    This is the protocol stated as an end state rather than as a sequence of
    steps, and applied to what is actually there:

      * a figure nothing could ever fill is removed - a rubric line that names
        an action, a request for a serology titre, a view with nothing said
        about what it wants, or one nobody is asking for any more;
      * findings borrowed from the station's bedside examination to stand in
        for a scan are cleared, because they describe something else;
      * words that survive are published, since a station holding a description
        nobody has released has marks nobody can earn;
      * a question that asked for an investigation is bound to the figure that
        holds it, where one exists.

    Costs nothing: no searching, no model calls, and safe to run repeatedly.
    """
    from app.services.osce.coverage import _NON_VISUAL_RE, station_views

    removed = cleared = published = bound = 0
    recorded = (station.findings_elicited or station.findings or "").strip()

    # What the station is still asking for: the views its rubric needs, and the
    # investigations its questions are still promising. A question the reconcile
    # pass has restated is asking for nothing - it keeps its request only as a
    # key the ingested binder can match - so it does not hold a figure open.
    asked_for = {v.wanted_description.strip().lower() for v in station_views(station)}
    asked_for |= {
        str(p.get("image_wanted") or "").strip().lower()
        for p in (station.prompts or [])
        if p.get("image_wanted")
        and not p.get("image_search_exhausted")
        and not p.get("image_impossible")
    }
    asked_for.discard("")
    claimed = {i for p in (station.prompts or []) for i in bound_figure_ids(p)}

    for figure in list(station.figures):
        if figure.image_id is not None:
            continue
        wanted = (figure.wanted_description or "").strip()

        # Nothing will ever fill this. Leaving it in place spends a search on
        # every run and shows the user a card that cannot be actioned.
        if not wanted or _NON_VISUAL_RE.match(wanted) or unsourceable_reason(wanted):
            db.delete(figure)
            removed += 1
            continue

        # Nobody is waiting for this one. It holds no image and no words, no
        # question shows it, and nothing on the station is still asking for what
        # it wanted - station 183 wants an OCT for a question that now states
        # the torsion in words, and station 164 wants a biometry printout no
        # question ever reads. Kept, they are counted for ever as views the
        # candidate met with nothing, which is what they are not.
        #
        # Words are what make the difference: a figure that states its findings
        # IS what the candidate meets, however little else is true of it.
        if (
            figure.id not in claimed
            and wanted.lower() not in asked_for
            and not (figure.described_findings or "").strip()
        ):
            db.delete(figure)
            removed += 1
            continue

        words = (figure.described_findings or "").strip()
        if words:
            borrowed = (
                words == recorded
                and verbatim_findings_floor(station, wanted)[0] is None
            )
            if borrowed or leaked_term(words, station):
                figure.described_findings = None
                figure.described_findings_approved = False
                cleared += 1
            elif not figure.described_findings_approved:
                figure.described_findings_approved = True
                published += 1

    db.flush()

    # A question's investigation, where the station already holds it.
    prompts = list(station.prompts or [])
    claimed = {i for p in prompts for i in bound_figure_ids(p)}
    # Words answer a question as well as a picture does. Sourcing binds only
    # what it attached, so a figure that found no image and had its findings
    # stated instead was left unbound and the question went on showing nothing:
    # station 259 holds a description of the specular microscopy its question C
    # asks for, written, approved, and attached to nobody.
    spare = [f for f in station.figures if answers_a_view(f) and f.id not in claimed]
    for prompt in prompts:
        wanted = str(prompt.get("image_wanted") or "").strip()
        if not wanted or bound_figure_ids(prompt) or prompt.get("image_impossible"):
            continue

        # The figure this question's own request created, matched on that
        # request. Exact, because the string was copied from the question -
        # and it is the only thing that can match a figure with no image,
        # which has no modality to compare.
        exact = next(
            (f for f in spare if (f.wanted_description or "").strip() == wanted), None
        )
        if exact is not None:
            prompt["figure_id"] = exact.id
            spare.remove(exact)
            bound += 1
            continue

        asked = named_modality(wanted)
        if asked is None:
            continue
        for figure in list(spare):
            if figure.image_id is None:
                continue  # nothing to read a modality from
            if (figure.modality or named_modality(figure.caption or "")) != asked:
                continue
            prompt["figure_id"] = figure.id
            spare.remove(figure)
            bound += 1
            break

    if bound:
        station.prompts = [dict(p) for p in prompts]
        flag_modified(station, "prompts")

    db.commit()
    return {"removed": removed, "cleared": cleared, "published": published, "bound": bound}


@register_handler(JOB_SETTLE_STATIONS)
def handle_settle_stations(ctx: JobContext) -> bool:
    """One station per chunk: hold the protocol's end state, cheaply."""
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
            outcome = settle_station(ctx.db, station)
            tally = dict((ctx.job.result or {}).get("settled", {}))
            for key, value in outcome.items():
                tally[key] = tally.get(key, 0) + value
            ctx.set_result(settled=tally)
        except Exception as exc:  # noqa: BLE001 - one station must not stop the pass
            ctx.db.rollback()
            logger.exception("Could not settle station %s", station.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"station_id": station.id})

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Settling: {index + 1} of {len(station_ids)}")
    return index + 1 >= len(station_ids)
