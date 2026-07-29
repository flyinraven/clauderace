"""What a document produced, and what deleting it takes with it.

An OSCE report produces stations, not questions. Counting only questions
reported "0 items" against a document that had just created 18 stations, and
told an administrator deleting it that nothing depended on it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import OsceStation, Question, QuestionPart, SourceDocument
from tests.conftest import auth


def _document(db: Session, kind: str) -> SourceDocument:
    doc = SourceDocument(
        filename=f"2026 Semester 1 {kind.upper()}.pdf", content_type="application/pdf",
        sha256=f"{kind}-digest", size_bytes=1024, data=b"%PDF-1.4",
        page_count=115, exam_period="2026 Semester 1", document_kind=kind,
        status="completed",
    )
    db.add(doc)
    db.commit()
    return doc


def _survives(db, model, pk: int) -> bool:
    """The API deletes through its own session, so drop the identity map."""
    db.expunge_all()
    return db.query(model).filter(model.id == pk).first() is not None


def _count_for(client, admin, document_id: int) -> int:
    listing = client.get("/api/documents", headers=auth(admin)).json()
    return next(d["question_count"] for d in listing if d["id"] == document_id)


def test_an_osce_document_counts_the_stations_it_created(client, db, admin) -> None:
    doc = _document(db, "osce")
    for number in range(1, 19):
        db.add(OsceStation(station_number=number, total_marks=20,
                           source="past_paper", status="review",
                           source_document_id=doc.id))
    db.commit()

    assert _count_for(client, admin, doc.id) == 18


def test_a_written_document_still_counts_its_questions(client, db, admin) -> None:
    doc = _document(db, "written")
    for _ in range(3):
        question = Question(question_type="SEQ", stem="Discuss.", total_marks=10,
                            source="ingested", status="approved",
                            model_answer_status="none", source_document_id=doc.id)
        db.add(question)
        db.flush()
        db.add(QuestionPart(question_id=question.id, label="a", position=0,
                            text="List four causes.", marks=4))
    db.commit()

    assert _count_for(client, admin, doc.id) == 3


def test_a_document_that_produced_nothing_counts_zero(client, db, admin) -> None:
    assert _count_for(client, admin, _document(db, "osce").id) == 0


def test_deleting_an_osce_document_is_refused_until_the_stations_are_confirmed(
    client, db, admin
) -> None:
    doc = _document(db, "osce")
    db.add(OsceStation(station_number=1, total_marks=20, source="past_paper",
                       status="review", source_document_id=doc.id))
    db.commit()

    document_id = doc.id
    refused = client.delete(f"/api/documents/{document_id}", headers=auth(admin))
    assert refused.status_code == 409
    assert "1 item(s)" in refused.json()["detail"]
    assert _survives(db, SourceDocument, document_id)

    forced = client.delete(
        f"/api/documents/{document_id}?delete_questions=true", headers=auth(admin)
    )
    assert forced.status_code == 204
    assert not _survives(db, SourceDocument, document_id)
    assert db.query(OsceStation).count() == 0, "the stations went with it"
