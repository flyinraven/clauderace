"""Words for a station no photograph can be found for.

The examiner states the findings aloud instead. The model declining to invent
is a correct outcome; a provider error that never asked is not, and
`DescriptionUnavailable` is what keeps the two apart.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import OsceFigure, OsceStation
from app.services.ai import AIClient, AIError
from app.services.errors import log_error
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.osce.station_images.constants import (
    JOB_DESCRIBE_STATION_FIGURES,
    JOB_SETTLE_STATIONS,
)
from app.services.osce.station_images.verify import (
    grounding_problem,
    leaked_term,
    verbatim_findings_floor,
)

logger = logging.getLogger(__name__)


class DescriptionUnavailable(RuntimeError):
    """The description could not be attempted - not a decision, a failure.

    The model declining to invent is a correct outcome and leaves the figure
    with no words. So does an API error, and for an evening the two were
    reported identically: a provider misroute produced 47 "no words" results,
    a job that finished cleanly, and a bank of stations quietly left empty.
    """


DESCRIBE_SYSTEM = """You are the examiner at an ophthalmology OSCE station. No photograph exists of
what the candidate is meant to look at, so you state the findings aloud, as the
patient in front of them would have demonstrated.

You are given this station's recorded findings, its confirmed diagnosis, and the
rubric points the candidate is marked on. The recorded findings come first:
where they state something, state it as written and never contradict it.

Where they are silent on what the rubric asks the candidate to identify, state
what THIS patient would demonstrate given the confirmed diagnosis. That is not
invention: the diagnosis is established and its signs follow from it, and a
station whose rubric marks the pupil and the lid must put a pupil and a lid in
front of the candidate. Cover the whole of what the rubric asks about - the
primary position, the limitation in each direction of gaze, the lid, the pupil,
whatever it names - so that every mark can be earned by someone looking.

Do not invent measurements, laterality or severity that neither the record nor
the diagnosis settles: leave that detail out rather than choosing one. If the
findings say the fundus is normal, the fundus is normal.

Write 1-4 short sentences in the present tense, in the words an examiner would
use at the bedside. Report raw appearances and measurements only.

Do NOT characterise, classify or interpret what you report. Say what is seen,
not what it amounts to. Naming the pattern is the candidate's job and the thing
being marked - so no "congruous", "incongruous", "macular sparing", "consistent
with", "suggestive of", "in keeping with", "typical of", "pathognomonic", and no
naming of the diagnosis, syndrome, causative organism or underlying disease.

Do not mention management, investigations, prognosis or history. Do not say that
an image is missing or refer to a photograph.

If neither the findings nor the diagnosis tells you what this patient shows,
return an empty description rather than filling the gap.

Return ONLY a JSON object: {"description": "..."}"""


def describe_findings(
    client: AIClient, station: OsceStation, wanted: str | None
) -> tuple[str | None, str | None]:
    """State the signs aloud, for what no photograph could be found for.

    The station's own recorded findings are the source of truth, and the rubric
    only says which of them to cover. Given the rubric alone this wrote fluent,
    confident and wrong examination findings - horizontal motility defects for a
    station about elevation, an orthophoric cover test for a station about a
    squint. It had nothing to be faithful to, so it invented.

    Marked with the model answer task, not the utility one. This is text a
    candidate is examined on, not a mechanical rewording.
    """
    rubric_points = (wanted or "").strip()
    truth = (station.findings_elicited or station.findings or "").strip()
    # The diagnosis alone is enough to describe from: its signs follow from it,
    # and a station whose findings are one terse line is exactly the case this
    # exists for.
    if not truth and not rubric_points and not (station.diagnosis or "").strip():
        return None, None
    try:
        data = client.complete_json(
            task="model_answer",
            system=DESCRIBE_SYSTEM,
            user=(
                f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n\n"
                f"THIS STATION'S RECORDED FINDINGS - authoritative where they speak:\n"
                f"{truth or '(none recorded)'}\n\n"
                f"CONFIRMED DIAGNOSIS - established, so its signs are not a guess. "
                f"Never name it, or any of its terms:\n"
                f"{station.diagnosis or '(not recorded)'}\n\n"
                f"WHAT THE CANDIDATE IS MARKED ON - every one of these must be "
                f"earnable by someone looking at the patient you describe:\n"
                f"{rubric_points or '(not specified)'}\n\n"
                f"State what this patient demonstrates."
            ),
            # Enough for the description AND whatever the model thinks first.
            # At 320 the reply was cut off mid-JSON and every station came back
            # "Could not describe findings" - a parse failure reported as the
            # model declining, which is why 47 stations looked like refusals.
            max_tokens=900,
            temperature=0.0,
        )
    except (AIError, ValueError, AttributeError) as exc:
        # NOT the same as declining, and it must never again be reported as if
        # it were. Returning "no description" here made 47 HTTP 404s indis-
        # tinguishable from 47 stations the model had rightly refused to invent
        # for: the job finished, said "described 0, failed 0", and looked like
        # a healthy no-op while nothing worked at all.
        logger.warning(
            "Could not describe findings for station %s: %s", station.id, exc
        )
        raise DescriptionUnavailable(str(exc)) from exc

    text = str((data or {}).get("description") or "").strip()
    if not text:
        # The model is told to return nothing rather than fill a gap, so this
        # is a legitimate outcome - but it used to be the one path that logged
        # nothing at all, which made an absent description indistinguishable
        # from a rejected one.
        logger.info(
            "No description for station %s: the model returned none for %r",
            station.id, (rubric_points or truth)[:120],
        )
        return None, None
    leak = leaked_term(text, station)
    if leak:
        logger.warning(
            "Discarded a description of station %s: it gave away %r", station.id, leak
        )
        return None, None
    # Advisory, not a veto. Three runs in a row it discarded a correct
    # description for using ordinary examination words the findings happened
    # not to contain - "larger" and "constricts" for a dilated pupil with
    # light-near dissociation, then "convergence" for how the near response is
    # tested. Each time the answer was to widen a list that will never be
    # complete, while the station went on having nothing to show.
    #
    # It cannot distinguish paraphrase from invention, so it now tells the
    # reviewer what to look at instead of deciding for them. Naming the
    # diagnosis is still a hard reject above: that one is unambiguous.
    problem = grounding_problem(text, station, rubric_points)
    if problem:
        logger.info("Description of station %s wants checking: %s", station.id, problem)
    return text, problem


def figures_needing_description(db: Session) -> list[int]:
    """Figures with no image, whose station therefore has nothing to show.

    A figure reaches this state two ways: every candidate was rejected as the
    wrong investigation, or an administrator turned the image down. Either way
    the marks behind it cannot be earned until the findings are stated in
    words, which is the protocol's last resort.

    Already-described figures are skipped, so the pass can be re-run after a
    sourcing round without paying twice for the same station.
    """
    return sorted(
        f.id
        for f in db.execute(
            select(OsceFigure).where(OsceFigure.image_id.is_(None))
        ).scalars()
        if not (f.described_findings or "").strip()
    )


@register_handler(JOB_DESCRIBE_STATION_FIGURES)
def handle_describe_station_figures(ctx: JobContext) -> bool:
    """One figure per chunk: state the findings for a view with no image.

    Deliberately no searching. This is for figures whose sourcing is already
    finished and came back empty, so it spends no image-search quota and costs
    one `model_answer` call each.
    """
    figure_ids: list[int] = ctx.payload.get("figure_ids") or []
    if not figure_ids:
        raise JobHandlerError("No figure_ids supplied")

    if not ctx.job.total_steps:
        ctx.set_total(len(figure_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(figure_ids):
        return True

    figure = ctx.db.get(OsceFigure, figure_ids[index])
    station = ctx.db.get(OsceStation, figure.station_id) if figure else None
    if figure is not None and station is not None and figure.image_id is None:
        try:
            described, concern = describe_findings(
                AIClient(ctx.db), station, figure.wanted_description
            )
        except DescriptionUnavailable as exc:
            # Every figure will hit the same wall - a bad model id, a provider
            # that serves none of them, an exhausted key - so failing the job
            # is the honest report. Finishing 47 times and calling it "no
            # words" is what hid this for an evening.
            ctx.db.rollback()
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"figure_id": figure.id, "station_id": station.id})
            raise JobHandlerError(
                f"Could not write any findings: {exc}. Nothing was described - "
                f"check Admin > Settings that the model id is one your provider "
                f"serves."
            ) from exc
            if not described:
                # Almost every figure that reaches this job has no
                # `wanted_description`: it was written by ingest, or the view it
                # names was cleared. The rubric section of the prompt then reads
                # "(not specified)", and a model told to return nothing rather
                # than invent returns nothing - 185 times in a row, the first
                # time this ran.
                #
                # The station's recorded findings are what the examiners
                # printed, so stating them verbatim is not an invention and is
                # always available. Same floor the sourcing path already falls
                # back to, and the leak guard still applies.
                described, concern = verbatim_findings_floor(
                    station, figure.wanted_description
                )
            if described:
                figure.described_findings = described
                figure.verification_status = "described"
                # The leak guard inside describe_findings is the check that
                # matters and it is a hard reject: nothing reaching here names
                # the diagnosis. `grounding_problem` is advisory by design - it
                # threw away correct descriptions three runs running - so it is
                # written down to be read, not used to withhold the station.
                # Holding these for approval is what left stations with marks
                # no candidate could earn while the words sat unread.
                figure.described_findings_approved = True
                if concern:
                    figure.verification_notes = (
                        f"{figure.verification_notes or ''}  "
                        f"[Check the stated findings: {concern}]"
                    )[:4000]
                done = list((ctx.job.result or {}).get("described", []))
                done.append(figure.id)
                ctx.set_result(described=done)
            else:
                # The model returns nothing rather than invent, and a
                # description that named the diagnosis was discarded. Both are
                # correct outcomes and neither is retried here.
                empty = list((ctx.job.result or {}).get("no_words", []))
                empty.append(figure.id)
                ctx.set_result(no_words=empty)
            ctx.db.commit()
        except Exception as exc:  # noqa: BLE001 - one figure must not stop the pass
            ctx.db.rollback()
            logger.exception("Could not describe figure %s", figure.id)
            log_error(ctx.db, source="osce_images", message=str(exc),
                      context={"figure_id": figure.id, "station_id": station.id})
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(figure.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Describing: {index + 1} of {len(figure_ids)}")

    finished = index + 1 >= len(figure_ids)
    if finished:
        _queue_settle(ctx, sorted({
            f.station_id for f in ctx.db.execute(
                select(OsceFigure).where(OsceFigure.id.in_(figure_ids))
            ).scalars().all()
        }))
    return finished


def _queue_settle(ctx: JobContext, station_ids: list[int]) -> None:
    """The last word on a station: what is there must match the protocol."""
    from app.services.jobs.runner import create_job

    if not station_ids:
        return
    job = create_job(
        ctx.db,
        JOB_SETTLE_STATIONS,
        payload={"station_ids": station_ids},
        created_by_id=ctx.job.created_by_id,
        total_steps=len(station_ids),
        message=f"Settling {len(station_ids)} station(s)",
    )
    logger.info("Queued settle job %s for %d station(s)", job.id, len(station_ids))
