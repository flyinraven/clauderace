"""Upload and ingestion of past papers and examiners' reports."""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.models import Question, SourceDocument
from app.services.ingest import detect_document_kind, extract_document
from app.services.ingest.pipeline import JOB_INGEST_DOCUMENT
from app.services.jobs.runner import create_job

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = (".pdf", ".docx", ".txt", ".json", ".md")


class DocumentOut(BaseModel):
    id: int
    filename: str
    content_type: str
    size_bytes: int
    page_count: int | None
    exam_period: str | None
    document_kind: str | None
    status: str
    status_detail: str | None
    question_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class UploadResponse(BaseModel):
    document: DocumentOut
    job_id: int
    detected_kind: str
    detected_blocks: int


@router.get("", response_model=list[DocumentOut])
def list_documents(admin: AdminUser, db: DbSession) -> list[DocumentOut]:
    docs = db.execute(
        select(SourceDocument).order_by(SourceDocument.created_at.desc())
    ).scalars().all()
    counts = dict(
        db.execute(
            select(Question.source_document_id, func.count(Question.id))
            .where(Question.source_document_id.is_not(None))
            .group_by(Question.source_document_id)
        ).all()
    )
    out: list[DocumentOut] = []
    for doc in docs:
        item = DocumentOut.model_validate(doc)
        item.question_count = counts.get(doc.id, 0)
        out.append(item)
    return out


@router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    admin: AdminUser,
    db: DbSession,
    file: UploadFile = File(...),
    exam_period: str | None = Form(default=None),
    document_kind: str | None = Form(default=None),
    start_ingestion: bool = Form(default=True),
) -> UploadResponse:
    filename = file.filename or "upload"
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Accepted: {', '.join(ALLOWED_SUFFIXES)}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(data) // 1024 // 1024} MB; the limit is "
                   f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB",
        )

    digest = hashlib.sha256(data).hexdigest()
    existing = db.execute(
        select(SourceDocument).where(SourceDocument.sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This exact file was already uploaded as '{existing.filename}' "
                   f"(document {existing.id}).",
        )

    # Parse up front so the upload fails fast on an unreadable file, and so the
    # response can tell the administrator what was detected.
    try:
        parsed = extract_document(data, filename, file.content_type or "")
    except Exception as exc:  # noqa: BLE001 - surfaced to the uploader
        raise HTTPException(status_code=400, detail=f"Could not read this file: {exc}") from exc

    from app.services.ingest.segment import segment

    detected_kind, blocks = segment(parsed, document_kind)

    document = SourceDocument(
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        sha256=digest,
        size_bytes=len(data),
        data=data,
        page_count=parsed.page_count,
        exam_period=exam_period,
        document_kind=document_kind or detected_kind,
        status="uploaded",
        status_detail=(
            f"Detected {len(blocks)} item(s) and "
            f"{len(parsed.images)} clinical figure(s)."
        ),
        extracted_text=parsed.full_text[:1_000_000],
        uploaded_by_id=admin.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    job_id = 0
    if start_ingestion:
        job = create_job(
            db,
            JOB_INGEST_DOCUMENT,
            payload={"document_id": document.id},
            created_by_id=admin.id,
            total_steps=len(blocks),
            message="Queued for ingestion",
        )
        job_id = job.id

    return UploadResponse(
        document=DocumentOut.model_validate(document),
        job_id=job_id,
        detected_kind=detected_kind,
        detected_blocks=len(blocks),
    )


@router.post("/{document_id}/reingest", status_code=status.HTTP_202_ACCEPTED)
def reingest(document_id: int, admin: AdminUser, db: DbSession) -> dict[str, int]:
    document = db.get(SourceDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    document.status = "uploaded"
    document.status_detail = "Re-queued for ingestion"
    db.commit()

    job = create_job(
        db,
        JOB_INGEST_DOCUMENT,
        payload={"document_id": document.id},
        created_by_id=admin.id,
        message="Queued for re-ingestion",
    )
    return {"job_id": job.id}


@router.get("/{document_id}/preview")
def preview(document_id: int, admin: AdminUser, db: DbSession) -> dict:
    """Show what segmentation found, without calling the model.

    Useful for checking a new document format before spending tokens on it.
    """
    document = db.get(SourceDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    from app.services.ingest.segment import segment

    parsed = extract_document(document.data, document.filename, document.content_type)
    kind, blocks = segment(parsed, document.document_kind)
    return {
        "kind": kind,
        "page_count": parsed.page_count,
        "figures_kept": len(parsed.images),
        "figures_discarded": parsed.discarded_images,
        "blocks": [
            {
                "label": block.label,
                "pages": [block.page_numbers[0], block.page_numbers[-1]] if block.page_numbers else [],
                "characters": len(block.text),
                "figures": len(block.images),
                "preview": block.text[:400],
            }
            for block in blocks
        ],
    }


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: int, admin: AdminUser, db: DbSession, delete_questions: bool = False
) -> None:
    document = db.get(SourceDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    questions = db.execute(
        select(Question).where(Question.source_document_id == document_id)
    ).scalars().all()
    if questions and not delete_questions:
        raise HTTPException(
            status_code=409,
            detail=f"{len(questions)} question(s) came from this document. Pass "
                   f"delete_questions=true to remove them as well.",
        )
    for question in questions:
        db.delete(question)
    db.delete(document)
    db.commit()
