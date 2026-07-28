"""Find, download and attach clinical images to questions.

Everything fetched from the web records its source URL and, where the provider
supplies one, its licence. Web-sourced images land unapproved so an
administrator reviews them before candidates see them.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Figure, Image, Question
from app.services.ai import AIClient, ImagePart, TextPart
from app.services.errors import log_error
from app.services.imagesearch.base import ImageCandidate, ImageSearchError
from app.services.imagesearch.relevance import expected_modalities, modality_mismatch
from app.services.imagesearch.providers import (
    BraveImageSearch,
    OpenverseImageSearch,
    SerpApiImageSearch,
)
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler
from app.services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

JOB_ATTACH_IMAGES = "attach_images"

QUOTA_SETTING_KEY = "imagesearch.usage"
MIN_DIMENSION_PX = 250
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def build_provider(store: SettingsStore):
    name = store.get_str("imagesearch.provider", "brave").lower()
    if name == "none":
        return None
    if name == "brave":
        return BraveImageSearch(store.get_str("imagesearch.api_key", ""))
    if name == "serpapi":
        return SerpApiImageSearch(store.get_str("imagesearch.api_key", ""))
    if name == "openverse":
        return OpenverseImageSearch()
    raise ImageSearchError(f"Unknown image search provider '{name}'")


# --- Quota ----------------------------------------------------------------
def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def check_and_consume_quota(db: Session, store: SettingsStore) -> None:
    """Enforce a monthly query ceiling.

    Brave bills overages with no spending cap of its own, so this is the only
    thing standing between a runaway batch job and a surprise invoice.
    """
    limit = store.get_int("imagesearch.monthly_query_limit", 500)
    if limit <= 0:
        return

    usage = store.get(QUOTA_SETTING_KEY) or {}
    if not isinstance(usage, dict) or usage.get("month") != _current_month():
        usage = {"month": _current_month(), "count": 0}

    if usage["count"] >= limit:
        raise ImageSearchError(
            f"Monthly image search limit of {limit} queries has been reached. "
            f"Raise 'Monthly query limit' in Admin > Settings to continue."
        )

    usage["count"] = int(usage["count"]) + 1
    store.set(QUOTA_SETTING_KEY, usage)
    db.commit()


def quota_status(db: Session) -> dict[str, Any]:
    store = SettingsStore(db)
    usage = store.get(QUOTA_SETTING_KEY) or {}
    used = int(usage.get("count", 0)) if usage.get("month") == _current_month() else 0
    limit = store.get_int("imagesearch.monthly_query_limit", 500)
    return {"month": _current_month(), "used": used, "limit": limit, "remaining": max(0, limit - used)}


# --- Query building -------------------------------------------------------
def build_query(db: Session, question: Question, figure: Figure) -> str:
    """Compose a search query for a figure.

    Uses the model to turn the clinical context into the phrase a clinician
    would actually search, because "Figure 1" plus a stem is not a query.
    """
    hints = [
        part
        for part in (
            figure.caption,
            figure.wanted_description,
            question.topic,
            question.subspecialty,
        )
        if part
    ]
    fallback = " ".join(hints[:3]) or (question.topic or "ophthalmology clinical photograph")

    try:
        client = AIClient(db)
        if not client.is_configured_for("structuring"):
            return fallback
        response = client.complete(
            task="structuring",
            system=(
                "You write short image search queries for ophthalmology teaching "
                "material. Given a clinical scenario and a figure caption, reply "
                "with ONLY the search phrase - 3 to 8 words, no punctuation, no "
                "quotes. Name the specific imaging modality and diagnosis where "
                "known, e.g. 'fundus fluorescein angiogram Coats disease "
                "telangiectasia'."
            ),
            user=(
                f"Subspecialty: {question.subspecialty or 'unknown'}\n"
                f"Topic: {question.topic or 'unknown'}\n"
                f"Figure caption: {figure.caption or figure.wanted_description or 'none'}\n"
                f"Clinical stem: {(question.stem or '')[:800]}"
            ),
            max_tokens=40,
            temperature=0.2,
        )
        query = response.text.strip().strip('"').replace("\n", " ")
        return query or fallback
    except Exception:  # noqa: BLE001 - a query is better than no attempt
        logger.debug("Falling back to heuristic image query")
        return fallback


# --- Download and store ---------------------------------------------------
def download_candidate(candidate: ImageCandidate) -> tuple[bytes, str, int, int] | None:
    """Fetch and validate an image. Returns None if it is unusable."""
    try:
        with httpx.Client(timeout=25, follow_redirects=True) as client:
            response = client.get(
                candidate.image_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RACE-Exam-Simulator/0.1)"},
            )
    except httpx.HTTPError:
        return None

    if response.status_code >= 400:
        return None

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return None

    data = response.content
    if not data or len(data) > MAX_DOWNLOAD_BYTES:
        return None

    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as im:
            width, height = im.size
            im.verify()
    except Exception:  # noqa: BLE001 - corrupt or unsupported image
        return None

    if min(width, height) < MIN_DIMENSION_PX:
        return None

    return data, content_type, width, height


def attach_image_to_figure(
    db: Session, figure: Figure, candidate: ImageCandidate, downloaded: tuple[bytes, str, int, int]
) -> Image:
    data, content_type, width, height = downloaded
    digest = hashlib.sha256(data).hexdigest()

    existing = db.execute(select(Image).where(Image.sha256 == digest)).scalar_one_or_none()
    if existing is None:
        existing = Image(
            sha256=digest,
            content_type=content_type,
            data=data,
            width=width,
            height=height,
            size_bytes=len(data),
            origin="web",
            source_url=candidate.image_url,
            source_page_url=candidate.page_url,
            attribution=candidate.attribution or candidate.provenance,
            licence=candidate.licence,
            # Web-sourced images await administrator review; only figures lifted
            # from the user's own uploaded papers are auto-approved.
            is_approved=False,
        )
        db.add(existing)
        db.flush()

    figure.image_id = existing.id
    db.commit()
    return existing


CLASSIFY_SYSTEM = """\
You name what kind of clinical image you have been given, for an ophthalmology
examination question.

Answer with exactly one of: external, slit_lamp, fundus, angiogram, oct,
ultrasound, radiology, visual_field, topography, pathology, other.

Report what the image IS, not what you think was wanted. Use "other" for
diagrams, illustrations, graphs, stock photography, or anything that is not a
clinical image of a patient.

Return ONLY a JSON object: {"modality": "<value>", "is_clinical_image": true|false}"""


def classify_modality(db: Session, data: bytes, media_type: str) -> dict[str, Any]:
    """Ask a vision model what the image is. Empty dict if it cannot say."""
    try:
        client = AIClient(db)
        if not client.is_configured_for("vision"):
            return {}
        out = client.complete_json(
            task="vision",
            system=CLASSIFY_SYSTEM,
            user=[
                TextPart("Name this image's modality."),
                ImagePart(data=data, media_type=media_type),
            ],
            max_tokens=80,
            temperature=0.0,
        )
        return out if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001 - an unclassified image is not a failure
        logger.debug("Could not classify a candidate image's modality")
        return {}


def find_and_attach(db: Session, figure: Figure, question: Question) -> dict[str, Any]:
    store = SettingsStore(db)
    provider = build_provider(store)
    if provider is None:
        raise ImageSearchError("Image search is disabled (provider is set to 'none').")

    check_and_consume_quota(db, store)

    query = build_query(db, question, figure)
    candidates = provider.search(query, store.get_int("imagesearch.results_per_query", 6))
    if not candidates:
        return {"attached": False, "query": query, "reason": "no results"}

    # What the question asks the candidate to read decides what kind of image
    # can answer it. Attaching the first thing that downloaded is how a question
    # about the anterior segment ends up illustrated with an angiogram.
    expected = expected_modalities(
        figure.wanted_description, figure.caption, question.stem, question.topic
    )
    skipped: list[str] = []

    for candidate in candidates:
        downloaded = download_candidate(candidate)
        if downloaded is None:
            continue
        blob, content_type, _width, _height = downloaded

        verdict = classify_modality(db, blob, content_type)
        if verdict:
            if verdict.get("is_clinical_image") is False:
                skipped.append(f"{candidate.source or 'source'}: not a clinical image")
                continue
            mismatch = modality_mismatch(expected, verdict.get("modality"))
            if mismatch:
                skipped.append(f"{candidate.source or 'source'}: {mismatch}")
                continue

        image = attach_image_to_figure(db, figure, candidate, downloaded)
        return {
            "attached": True,
            "query": query,
            "image_id": image.id,
            "source_url": candidate.image_url,
            "page_url": candidate.page_url,
            "modality": verdict.get("modality"),
            "skipped": len(skipped),
        }

    return {
        "attached": False,
        "query": query,
        "reason": "; ".join(skipped[:5]) or "no candidate could be downloaded",
    }


# --- Job handler ----------------------------------------------------------
@register_handler(JOB_ATTACH_IMAGES)
def handle_attach_images(ctx: JobContext) -> bool:
    """Attach an image to each figure that has none, one figure per chunk."""
    figure_ids: list[int] = ctx.payload.get("figure_ids") or []
    if not figure_ids:
        raise JobHandlerError("No figure_ids supplied")

    if not ctx.job.total_steps:
        ctx.set_total(len(figure_ids))

    index = ctx.cursor_get("index", 0)
    if index >= len(figure_ids):
        return True

    figure = ctx.db.get(Figure, figure_ids[index])
    if figure is not None and figure.image_id is None:
        question = ctx.db.get(Question, figure.question_id)
        try:
            outcome = find_and_attach(ctx.db, figure, question) if question else {"attached": False}
            key = "attached" if outcome.get("attached") else "skipped"
            done = list((ctx.job.result or {}).get(key, []))
            done.append(figure.id)
            ctx.set_result(**{key: done})
        except ImageSearchError as exc:
            # Quota and credential problems affect every remaining figure, so
            # stop the batch rather than burning through it failing.
            ctx.db.rollback()
            log_error(ctx.db, source="imagesearch", message=str(exc), context={"figure_id": figure.id})
            raise JobHandlerError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            ctx.db.rollback()
            logger.exception("Image attach failed for figure %s", figure.id)
            log_error(ctx.db, source="imagesearch", message=str(exc), context={"figure_id": figure.id})
            failed = list((ctx.job.result or {}).get("failed", []))
            failed.append(figure.id)
            ctx.set_result(failed=failed)

    ctx.cursor_set(index=index + 1)
    ctx.advance(1, f"Images: {index + 1} of {len(figure_ids)}")
    return index + 1 >= len(figure_ids)


def figures_needing_images(db: Session, question_id: int | None = None) -> list[int]:
    stmt = select(Figure.id).where(Figure.image_id.is_(None)).order_by(Figure.id)
    if question_id:
        stmt = stmt.where(Figure.question_id == question_id)
    return list(db.execute(stmt).scalars().all())
