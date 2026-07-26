"""Document ingestion: extract -> segment -> structure -> persist."""

from app.services.ingest.extract import ExtractedDocument, extract_document
from app.services.ingest.pipeline import JOB_INGEST_DOCUMENT, handle_ingest_document
from app.services.ingest.segment import Block, detect_document_kind, segment

__all__ = [
    "ExtractedDocument",
    "extract_document",
    "Block",
    "segment",
    "detect_document_kind",
    "JOB_INGEST_DOCUMENT",
    "handle_ingest_document",
]
