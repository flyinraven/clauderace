"""Deleting a question or a station that ingested badly.

Both refuse by default when something depends on them, because deletion takes
candidates' recorded work with it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import (
    ExamPaper,
    ExamPaperQuestion,
    OsceSession,
    OsceStation,
    Question,
    QuestionPart,
)
from tests.conftest import auth


def _exists(db, model, pk: int) -> bool:
    """The API deletes through its own session, so drop the identity map."""
    db.expire_all()
    return db.get(model, pk) is not None


def _question(db: Session) -> Question:
    question = Question(
        question_type="SEQ", stem="Describe the management of acute angle closure.",
        total_marks=10, source="ingested", status="approved", model_answer_status="none",
    )
    db.add(question)
    db.flush()
    db.add(QuestionPart(question_id=question.id, label="a", position=0,
                        text="List four causes.", marks=4))
    db.commit()
    return question


def _station(db: Session) -> OsceStation:
    station = OsceStation(station_number=1, title="Anterior segment", total_marks=20,
                          source="ingested", status="review")
    db.add(station)
    db.commit()
    return station


def test_admin_can_delete_a_question(client, db, admin) -> None:
    question = _question(db)
    question_id = question.id

    response = client.delete(f"/api/questions/{question_id}", headers=auth(admin))

    assert response.status_code == 204
    assert not _exists(db, Question, question_id)


def test_deleting_a_question_in_a_paper_is_refused_then_forced(client, db, admin) -> None:
    question = _question(db)
    question_id = question.id
    paper = ExamPaper(title="Paper 1", paper_type="SEQ", total_marks=100)
    db.add(paper)
    db.flush()
    db.add(ExamPaperQuestion(paper_id=paper.id, question_id=question_id,
                             section="A", position=0))
    db.commit()

    refused = client.delete(f"/api/questions/{question_id}", headers=auth(admin))
    assert refused.status_code == 409
    assert "paper" in refused.json()["detail"].lower()
    assert _exists(db, Question, question_id)

    forced = client.delete(
        f"/api/questions/{question_id}?remove_from_papers=true", headers=auth(admin)
    )
    assert forced.status_code == 204
    assert not _exists(db, Question, question_id)


def test_a_student_cannot_delete_a_question(client, db, student) -> None:
    question = _question(db)
    question_id = question.id

    response = client.delete(f"/api/questions/{question_id}", headers=auth(student))

    assert response.status_code == 403
    assert _exists(db, Question, question_id)


def test_admin_can_delete_a_station(client, db, admin) -> None:
    station = _station(db)
    station_id = station.id

    response = client.delete(f"/api/osce/stations/{station_id}", headers=auth(admin))

    assert response.status_code == 204
    assert not _exists(db, OsceStation, station_id)


def test_deleting_a_sat_station_is_refused_then_forced(client, db, admin, student) -> None:
    station = _station(db)
    station_id = station.id
    db.add(OsceSession(user_id=student.id, station_id=station_id, is_timed=True,
                       current_prompt_index=0, grading_status="none"))
    db.commit()

    refused = client.delete(f"/api/osce/stations/{station_id}", headers=auth(admin))
    assert refused.status_code == 409
    assert "sitting" in refused.json()["detail"].lower()
    assert _exists(db, OsceStation, station_id)

    forced = client.delete(
        f"/api/osce/stations/{station_id}?delete_sittings=true", headers=auth(admin)
    )
    assert forced.status_code == 204
    assert not _exists(db, OsceStation, station_id)
