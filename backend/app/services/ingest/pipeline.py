"""Ingestion job: uploaded document -> structured questions in the bank.

Runs one block per chunk so a 38-page report survives a Render restart: the
cursor records how many blocks are done, and the next tick picks up there.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    SOURCE_PAST_PAPER,
    STATUS_REVIEW,
    normalise_subspecialty,
)
from app.models import (
    ExaminerFeedback,
    Figure,
    Image,
    OsceFigure,
    OsceStation,
    Question,
    QuestionPart,
    SourceDocument,
)
from app.services.ai import AIClient
from app.services.errors import log_error
from app.services.ingest.extract import ExtractedDocument, extract_document
from app.services.ingest.segment import Block, segment
from app.services.ingest.structure import structure_osce_block, structure_written_block
from app.services.jobs.runner import JobContext, JobHandlerError, register_handler

logger = logging.getLogger(__name__)

JOB_INGEST_DOCUMENT = "ingest_document"

# Extracted documents are cached per job so each chunk does not re-parse the
# PDF. The cache is purely an optimisation - a cold process rebuilds it.
_BLOCK_CACHE: dict[int, tuple[ExtractedDocument, str, list[Block]]] = {}


@register_handler(JOB_INGEST_DOCUMENT)
def handle_ingest_document(ctx: JobContext) -> bool:
    document_id = ctx.payload.get("document_id")
    if not document_id:
        raise JobHandlerError("Ingestion job is missing document_id")

    source = ctx.db.get(SourceDocument, document_id)
    if source is None:
        raise JobHandlerError(f"Source document {document_id} no longer exists")

    doc, kind, blocks = _load_blocks(ctx.db, source)

    if not blocks:
        source.status = "failed"
        source.status_detail = (
            "No questions or stations could be identified. Expected headings such "
            "as 'SEQ 1' or 'Station 01'."
        )
        ctx.set_result(created=0, kind=kind)
        return True

    index = ctx.cursor_get("block_index", 0)

    if index == 0 and not ctx.cursor_get("cleared"):
        # Re-ingesting must replace, not duplicate. Anything previously created
        # from this document is removed before the first block is written.
        _clear_previous_output(ctx.db, source.id)
        ctx.cursor_set(cleared=True)

    if not ctx.job.total_steps:
        ctx.set_total(len(blocks))
        source.status = "processing"
        source.document_kind = kind
        source.page_count = doc.page_count
    if index >= len(blocks):
        return _finalise(ctx, source, kind)

    block = blocks[index]
    client = AIClient(ctx.db)

    try:
        if kind == "osce":
            structured = structure_osce_block(client, block, job_id=ctx.job.id)
            created_id = _persist_station(ctx.db, source, block, structured)
            key = "station_ids"
        else:
            structured = structure_written_block(client, block, job_id=ctx.job.id)
            created_id = _persist_question(ctx.db, source, block, structured)
            key = "question_ids"

        created = list(ctx.job.result.get(key, [])) if ctx.job.result else []
        created.append(created_id)
        ctx.set_result(**{key: created})

        for warning in structured.get("warnings", []):
            existing = list((ctx.job.result or {}).get("warnings", []))
            existing.append(f"{block.label}: {warning}")
            ctx.set_result(warnings=existing)

    except Exception as exc:  # noqa: BLE001 - one bad block must not kill the run
        logger.exception("Failed to structure %s", block.label)
        log_error(
            ctx.db,
            source="ingest",
            message=f"{block.label}: {exc}",
            context={"document_id": document_id, "block": block.label},
        )
        failures = list((ctx.job.result or {}).get("failed", []))
        failures.append(block.label)
        ctx.set_result(failed=failures)

    ctx.cursor_set(block_index=index + 1)
    ctx.advance(1, f"Processed {block.label} ({index + 1} of {len(blocks)})")

    if index + 1 >= len(blocks):
        return _finalise(ctx, source, kind)
    return False


def _finalise(ctx: JobContext, source: SourceDocument, kind: str) -> bool:
    result = ctx.job.result or {}
    created = len(result.get("question_ids", [])) + len(result.get("station_ids", []))
    failed = result.get("failed", [])
    source.status = "completed" if not failed else "completed_with_errors"
    source.status_detail = (
        f"Created {created} item(s)."
        + (f" Failed: {', '.join(failed)}." if failed else "")
    )
    ctx.set_result(created=created, kind=kind)
    ctx.set_message(source.status_detail)
    _BLOCK_CACHE.pop(source.id, None)

    if kind == "osce" and result.get("station_ids"):
        _queue_prompt_build(ctx, result["station_ids"])
    return True


def _queue_prompt_build(ctx: JobContext, station_ids: list[int]) -> None:
    """Chain the prompt build onto the ingest.

    An ingested station arrives flat - tasks and a rubric, but none of the
    turn-taking examiner questions a sitting needs, so it shows as "Not ready"
    until someone remembers to press Build prompts. Every ingest has needed it,
    so it is not really a separate decision.
    """
    from app.services.jobs.runner import create_job
    from app.services.osce.prompts import JOB_BUILD_OSCE_PROMPTS

    pending = [
        s.id
        for s in ctx.db.execute(
            select(OsceStation).where(
                OsceStation.id.in_(station_ids),
                OsceStation.prompts_status.in_(["none", "failed"]),
            )
        ).scalars().all()
    ]
    if not pending:
        return
    job = create_job(
        ctx.db,
        JOB_BUILD_OSCE_PROMPTS,
        payload={"station_ids": pending},
        created_by_id=ctx.job.created_by_id,
        total_steps=len(pending),
        message=f"Preparing {len(pending)} station(s)",
    )
    logger.info("Queued prompt build job %s for %d station(s)", job.id, len(pending))


def _clear_previous_output(db: Session, source_document_id: int) -> None:
    """Delete questions and stations previously created from this document.

    Extracted `Image` rows are deliberately left in place: they are
    content-addressed by sha256 and get re-linked on the next pass, so keeping
    them avoids re-decoding every figure.
    """
    questions = db.execute(
        select(Question).where(Question.source_document_id == source_document_id)
    ).scalars().all()
    for question in questions:
        db.delete(question)

    stations = db.execute(
        select(OsceStation).where(OsceStation.source_document_id == source_document_id)
    ).scalars().all()
    for station in stations:
        db.delete(station)

    if questions or stations:
        logger.info(
            "Cleared %d question(s) and %d station(s) before re-ingesting document %s",
            len(questions), len(stations), source_document_id,
        )
    db.commit()


def _load_blocks(db: Session, source: SourceDocument) -> tuple[ExtractedDocument, str, list[Block]]:
    cached = _BLOCK_CACHE.get(source.id)
    if cached:
        return cached

    doc = extract_document(source.data, source.filename, source.content_type)
    kind, blocks = segment(doc, source.document_kind)
    if source.extracted_text is None:
        source.extracted_text = doc.full_text[:1_000_000]
        db.commit()
    _BLOCK_CACHE[source.id] = (doc, kind, blocks)
    return doc, kind, blocks


# --- Persistence ----------------------------------------------------------
def _persist_question(
    db: Session, source: SourceDocument, block: Block, data: dict[str, Any]
) -> int:
    question = Question(
        question_type=data["question_type"],
        subspecialty=data.get("subspecialty"),
        topic=data.get("topic"),
        purpose=data.get("purpose"),
        stem=data.get("stem") or "",
        curriculum_standard_raw=data.get("curriculum_standard_raw"),
        curriculum_codes=data.get("curriculum_codes") or None,
        total_marks=int(data.get("total_marks") or 0),
        source=SOURCE_PAST_PAPER,
        source_document_id=source.id,
        exam_period=source.exam_period,
        original_number=block.number,
        # Past-paper questions land in review so an administrator can check the
        # transcription before candidates ever see them.
        status=STATUS_REVIEW,
        model_answer_status="none",
        generation_meta={"warnings": data.get("warnings", []), "source_block": block.label},
    )
    db.add(question)
    db.flush()

    parts_by_label: dict[str, QuestionPart] = {}
    for spec in data["parts"]:
        part = QuestionPart(
            question_id=question.id,
            label=spec.get("label"),
            position=spec.get("position", 0),
            text=spec["text"],
            marks=int(spec["marks"]) if float(spec["marks"]).is_integer() else spec["marks"],
            preamble=spec.get("preamble"),
        )
        db.add(part)
        db.flush()
        if part.label:
            parts_by_label[part.label.strip().lower().strip(")")] = part

    for spec in data.get("examiner_feedback", []):
        db.add(
            ExaminerFeedback(
                question_id=question.id,
                examiner_number=spec.get("examiner_number"),
                common_mistakes=spec.get("common_mistakes") or None,
                cohort_impression=spec.get("cohort_impression") or None,
            )
        )

    _attach_figures(db, question, block, data.get("figures", []), parts_by_label)

    db.commit()
    return question.id


def _attach_figures(
    db: Session,
    question: Question,
    block: Block,
    figure_specs: list[dict[str, Any]],
    parts_by_label: dict[str, QuestionPart],
) -> None:
    """Store extracted images and link them to the question in page order.

    The model's `figures` array supplies captions; the actual pixels come from
    the PDF. They are zipped positionally because both are in document order.
    """
    for index, image in enumerate(block.images):
        spec = figure_specs[index] if index < len(figure_specs) else {}
        record = _get_or_create_image(db, image, question.source_document_id)

        part_id = None
        referenced = (spec.get("referenced_in_part") or "").strip().lower().strip(")")
        if referenced and referenced in parts_by_label:
            part_id = parts_by_label[referenced].id

        db.add(
            Figure(
                question_id=question.id,
                image_id=record.id,
                part_id=part_id,
                position=index,
                label=spec.get("label") or image.label or f"Figure {index + 1}",
                caption=spec.get("caption") or image.caption,
            )
        )

    # Captions the model saw in the text but for which no image was extracted -
    # kept so an administrator can source one manually or via image search.
    for index in range(len(block.images), len(figure_specs)):
        spec = figure_specs[index]
        db.add(
            Figure(
                question_id=question.id,
                image_id=None,
                position=index,
                label=spec.get("label") or f"Figure {index + 1}",
                caption=spec.get("caption"),
                wanted_description=spec.get("caption"),
            )
        )


def _get_or_create_image(db: Session, extracted, source_document_id: int | None) -> Image:
    existing = db.execute(
        select(Image).where(Image.sha256 == extracted.sha256)
    ).scalar_one_or_none()
    if existing:
        return existing
    record = Image(
        sha256=extracted.sha256,
        content_type=extracted.content_type,
        data=extracted.data,
        width=extracted.width,
        height=extracted.height,
        size_bytes=len(extracted.data),
        origin="pdf",
        source_document_id=source_document_id,
        source_page_number=extracted.page_number,
        # Figures lifted from the candidate's own uploaded paper need no
        # separate approval; only web-sourced images do.
        is_approved=True,
    )
    db.add(record)
    db.flush()
    return record


def _persist_station(
    db: Session, source: SourceDocument, block: Block, data: dict[str, Any]
) -> int:
    station = OsceStation(
        station_number=data.get("station_number") or block.number,
        subspecialty=data.get("subspecialty") or normalise_subspecialty(data.get("title")),
        title=data.get("title"),
        # Without this a station opens with the generic "A patient is seated in
        # front of you", which tells the candidate nothing about who to expect.
        patient_demographic=data.get("patient_demographic"),
        case_summary=data.get("case_summary"),
        aims=data.get("aims") or None,
        patient_history=data.get("patient_history"),
        findings=data.get("findings"),
        diagnosis=data.get("diagnosis"),
        cohort_performance=data.get("cohort_performance"),
        common_mistakes=data.get("common_mistakes") or None,
        tasks=data.get("tasks") or None,
        rubric=data.get("rubric") or None,
        total_marks=int(data.get("total_marks") or 20),
        source=SOURCE_PAST_PAPER,
        source_document_id=source.id,
        exam_period=source.exam_period,
        status=STATUS_REVIEW,
    )
    db.add(station)
    db.flush()
    _attach_station_figures(db, source, block, station)
    db.commit()
    return station.id


def _attach_station_figures(
    db: Session, source: SourceDocument, block: Block, station: OsceStation
) -> None:
    """Show the report's own clinical photographs at the station.

    A station without an image cannot test visual recognition at all, and the
    OSCE deck carries the real photograph the candidates were shown. Dropping
    it and searching the web for a lookalike would be strictly worse: these
    are already faithful, and need no vision check or approval.
    """
    for position, extracted in enumerate(block.images):
        record = _get_or_create_image(db, extracted, source.id)
        db.add(
            OsceFigure(
                station_id=station.id,
                image_id=record.id,
                position=position,
                caption=extracted.caption or extracted.label,
                verification_status="verified",
                is_approved=True,
            )
        )


__all__ = ["JOB_INGEST_DOCUMENT", "handle_ingest_document"]
