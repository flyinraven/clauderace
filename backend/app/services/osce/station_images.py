"""Source and verify clinical images for OSCE stations.

The real OSCE puts a live patient in front of the candidate. A station with no
image cannot test visual recognition, so images are searched for using the
station's own findings - but a generic web photograph of "hypermature cataract"
will not show *this* patient's inferior subluxation. An image that contradicts
the rubric is worse than none, because the candidate is then marked down for
correctly describing what they can actually see.

Every candidate image is therefore checked by a vision model against the
station's elicited findings, and rejected unless it genuinely shows them.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Image, OsceFigure, OsceStation
from app.services.ai import AIClient, AIError, ImagePart, TextPart
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.imagesearch.base import ImageSearchError
from app.services.imagesearch.service import (
    attach_image_to_figure,
    build_provider,
    check_and_consume_quota,
    download_candidate,
)
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

JOB_SOURCE_STATION_IMAGES = "source_station_images"

# Below this a faithful match is not trustworthy enough to show as the patient.
MIN_MATCH_CONFIDENCE = 0.7
# A representative image only has to be a genuine, describable clinical image of
# the right pathology, so it clears a lower bar - but it is labelled as such.
MIN_REPRESENTATIVE_CONFIDENCE = 0.55

QUERY_SYSTEM = """\
You write image search queries for ophthalmology teaching material.

Given an OSCE station's case and the clinical signs the candidate is meant to
see, write THREE search phrases, from most specific to most general. Searching
only for the full picture fails on stations that combine several findings, but
the underlying pathologies are common and easy to find on their own.

  1. specific  - modality plus the distinctive combination of signs
  2. core      - modality plus the single main diagnosis
  3. broad     - the diagnosis or classic sign alone, no modality

Name the modality as a photographer would: "slit lamp photograph", "fundus
photograph", "external eye photograph", "OCT", "fluorescein angiogram".

Never include incidental surgical hardware, laterality, or a second unrelated
condition in phrases 2 and 3 - those are what make a search return nothing.

Return ONLY a JSON object:
{"queries": ["specific phrase", "core phrase", "broad phrase"]}"""

VERIFY_SYSTEM = """\
You are checking whether a photograph is suitable to show a candidate at an
ophthalmology OSCE station, where they will be asked to describe what they see.

You are given the station's clinical signs and one candidate image. Grade it
into one of three tiers.

"faithful"        - a genuine clinical image of the right modality that shows
                    the described signs. Ideal: the candidate can be marked
                    against the station's rubric as written.
"representative"  - a genuine clinical image of the right modality showing the
                    station's core pathology, but not every stated sign (for
                    example the right disease without the specific laterality,
                    severity, or an incidental surgical device). Still valuable
                    teaching material.
"reject"          - anything else.

ALWAYS reject:
  - diagrams, illustrations, cartoons, graphs, slides, tables
  - arrows, circles, asterisks or text annotations that point out the
    abnormality, since they do the candidate's describing for them
  - burned-in captions that name the diagnosis or the sign
  - watermarks, clinic branding, before-and-after marketing images
  - veterinary or non-human eyes
  - the wrong modality entirely (an OCT when an external photo is needed)
  - a different disease, or quality too poor to describe

DO NOT reject a multi-panel image merely for being multi-panel. A montage of
the nine positions of gaze is the standard, and often the only, way to
photograph ocular motility, cranial nerve palsies, Duane's syndrome and lid
disorders - such a montage is exactly what a candidate should be shown. Plain
panel letters with no explanatory text are acceptable; it is annotation that
identifies the abnormality which is not.

DO NOT reject on the patient's age, sex or ethnicity. The candidate is being
asked to describe a clinical sign, not to guess demographics, and a sign looks
the same whoever carries it. Congenital conditions in particular are almost
always published as photographs of children even when the station's patient is
an adult; that is not a mismatch.

Judge the image on its own merits, not on the page it came from.

Return ONLY a JSON object:
{
  "tier": "faithful" | "representative" | "reject",
  "confidence": <number 0-1>,
  "shows": "what the image actually shows, one sentence",
  "reason": "why you graded it that way, one sentence",
  "missing": "any station sign the image does NOT show, or null",
  "caption": "a neutral caption for the station, naming only the modality and
              laterality - it must NOT give away the diagnosis"
}"""


def build_search_queries(db: Session, client: AIClient, station: OsceStation) -> list[str]:
    """Three queries, specific to broad, tried in order until one yields a match."""
    signs = station.findings_elicited or station.findings or ""
    fallback = [
        q for q in [
            " ".join(p for p in [station.diagnosis, "clinical photograph"] if p),
            station.diagnosis or "",
            station.subspecialty or "ophthalmology clinical photograph",
        ] if q
    ]
    try:
        data = client.complete_json(
            task="utility",
            system=QUERY_SYSTEM,
            user=(
                f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n"
                f"CASE: {station.case_summary or 'unknown'}\n"
                f"SIGNS THE CANDIDATE SHOULD SEE: {signs or 'unknown'}\n"
                f"DIAGNOSIS (for your context only): {station.diagnosis or 'unknown'}"
            ),
            max_tokens=200,
            temperature=0.2,
        )
        queries = [
            str(q).strip().strip('"')
            for q in (data.get("queries") if isinstance(data, dict) else data) or []
            if str(q).strip()
        ]
        return queries or fallback
    except (AIError, ValueError, AttributeError):
        return fallback


def verify_image(
    db: Session, client: AIClient, station: OsceStation, data: bytes, media_type: str
) -> dict[str, Any]:
    """Ask a vision model whether this image really shows the station's signs."""
    signs = station.findings_elicited or station.findings or "(not recorded)"
    content = [
        TextPart(
            f"SUBSPECIALTY: {station.subspecialty or 'unknown'}\n"
            f"CASE: {station.case_summary or 'unknown'}\n\n"
            f"CLINICAL SIGNS THE CANDIDATE IS EXPECTED TO SEE:\n{signs}\n\n"
            f"Judge the image below."
        ),
        ImagePart(data=data, media_type=media_type),
    ]
    data_out = client.complete_json(task="vision", system=VERIFY_SYSTEM, user=content)
    if not isinstance(data_out, dict):
        raise ValueError("Image verification did not return a JSON object")
    return data_out


def source_image_for_station(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Find, verify and attach one image. Returns an outcome summary."""
    store = SettingsStore(db)
    provider = build_provider(store)
    if provider is None:
        raise ImageSearchError("Image search is disabled (provider is 'none').")

    figure = db.execute(
        select(OsceFigure).where(OsceFigure.station_id == station.id)
    ).scalars().first()
    if figure is None:
        figure = OsceFigure(
            station_id=station.id,
            position=0,
            wanted_description=station.findings_elicited or station.findings,
        )
        db.add(figure)
        db.flush()

    queries = build_search_queries(db, client, station)
    per_query = store.get_int("imagesearch.results_per_query", 6)
    rejections: list[str] = []
    best_representative: tuple[float, Any, Any, dict[str, Any]] | None = None

    # Never offer back an image the user has already turned down.
    already_rejected = set(figure.rejected_urls or [])
    auto_approve = store.get_bool("imagesearch.auto_approve", True)

    # Work specific -> broad, stopping at the first faithful match. A
    # representative hit found early is held in reserve rather than accepted
    # immediately, in case a later query turns up something faithful.
    for query in queries:
        figure.search_query = query
        try:
            check_and_consume_quota(db, store)
            candidates = provider.search(query, per_query)
        except ImageSearchError:
            raise
        if not candidates:
            rejections.append(f"no results for '{query}'")
            continue

        for candidate in candidates:
            if candidate.image_url in already_rejected:
                continue
            downloaded = download_candidate(candidate)
            if downloaded is None:
                continue
            blob, content_type, _width, _height = downloaded

            try:
                verdict = verify_image(db, client, station, blob, content_type)
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                logger.debug("Verification error on a candidate: %s", exc)
                continue

            tier = str(verdict.get("tier") or "reject").lower()
            confidence = as_float(verdict.get("confidence"), 0.0)

            if tier == "faithful" and confidence >= MIN_MATCH_CONFIDENCE:
                return _attach(db, figure, candidate, downloaded, verdict, "faithful",
                               confidence, query, len(rejections), auto_approve)

            if tier == "representative" and confidence >= MIN_REPRESENTATIVE_CONFIDENCE:
                if best_representative is None or confidence > best_representative[0]:
                    best_representative = (confidence, candidate, downloaded, verdict)
                continue

            rejections.append(
                f"{candidate.source or 'source'}: {verdict.get('reason') or 'rejected'}"
            )

    if best_representative is not None:
        confidence, candidate, downloaded, verdict = best_representative
        return _attach(db, figure, candidate, downloaded, verdict, "representative",
                       confidence, figure.search_query or queries[0], len(rejections),
                       auto_approve)

    figure.image_id = None
    figure.verification_status = "rejected"
    figure.verification_notes = (
        f"Tried {len(queries)} quer(ies): {'; '.join(queries)}. "
        + " | ".join(rejections[:5])
    )[:4000]
    db.commit()
    return {"attached": False, "queries": queries, "reason": "all candidates rejected",
            "rejected": len(rejections)}


def _attach(
    db: Session,
    figure: OsceFigure,
    candidate: Any,
    downloaded: Any,
    verdict: dict[str, Any],
    tier: str,
    confidence: float,
    query: str,
    rejected: int,
    auto_approve: bool = True,
) -> dict[str, Any]:
    # OsceFigure exposes the same `image_id` the writer sets, so the
    # deduplicate-and-store path is reused as-is.
    image = attach_image_to_figure(db, figure, candidate, downloaded)
    figure.image_id = image.id
    figure.caption = str(verdict.get("caption") or "").strip() or None
    figure.verification_status = tier
    notes = str(verdict.get("shows") or "").strip()
    missing = str(verdict.get("missing") or "").strip()
    if tier == "representative" and missing and missing.lower() not in {"null", "none"}:
        notes = f"{notes}  [Does NOT show: {missing}]"
    figure.verification_notes = notes or None
    figure.match_confidence = confidence
    figure.search_query = query
    # Vision verification has already discarded diagrams, wrong modalities and
    # unrelated pathology, so a verified image is shown straight away and the
    # user rejects the ones that are wrong. Holding everything back for
    # approval means stations start with no image at all, which is worse.
    figure.is_approved = auto_approve
    db.commit()
    return {
        "attached": True, "tier": tier, "query": query,
        "confidence": confidence, "source_url": candidate.image_url,
        "rejected": rejected,
    }


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
            outcome = source_image_for_station(
                ctx.db, AIClient(ctx.db), station, job_id=ctx.job.id
            )
            key = "attached" if outcome.get("attached") else "no_image"
            done = list((ctx.job.result or {}).get(key, []))
            done.append(station.id)
            ctx.set_result(**{key: done})
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
    return index + 1 >= len(station_ids)


def stations_needing_images(db: Session) -> list[int]:
    """Stations with no verified image yet."""
    with_image = set(
        db.execute(
            select(OsceFigure.station_id).where(OsceFigure.image_id.is_not(None))
        ).scalars().all()
    )
    all_ids = db.execute(select(OsceStation.id).order_by(OsceStation.id)).scalars().all()
    return [i for i in all_ids if i not in with_image]

