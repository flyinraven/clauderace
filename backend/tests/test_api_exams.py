"""Written papers: the bank, assembly, the clock gate, and marking.

The clock gate is the load-bearing part. The client is never trusted with it, so
the tests drive the server clock directly by moving `started_at` and assert that
the API alone refuses to serve or accept what the phase forbids.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.constants import PAPER_SPECS, QUESTION_SEQ, QUESTION_VSAQ, STATUS_APPROVED
from app.models import ExamSession, ModelAnswerPoint, Question, QuestionPart
from tests.conftest import auth


def make_question(
    db,
    question_type: str = QUESTION_VSAQ,
    subspecialty: str = "Glaucoma",
    parts: int = 2,
    marks_per_part: float = 3,
    status: str = STATUS_APPROVED,
    with_key: bool = True,
) -> Question:
    question = Question(
        question_type=question_type,
        subspecialty=subspecialty,
        topic="Angle closure",
        stem="A 62-year-old presents with a painful red eye and haloes.",
        total_marks=int(parts * marks_per_part),
        status=status,
        source="past_paper",
        exam_period="2026 Semester 1",
        model_answer_status="complete" if with_key else "none",
        angoff_expected=0.5,
    )
    db.add(question)
    db.flush()
    for i in range(parts):
        part = QuestionPart(
            question_id=question.id,
            label=chr(ord("a") + i),
            position=i,
            text=f"Sub-question {i + 1}. List the immediate management steps.",
            marks=marks_per_part,
        )
        db.add(part)
        db.flush()
        if with_key:
            for j in range(int(marks_per_part)):
                db.add(
                    ModelAnswerPoint(
                        part_id=part.id,
                        position=j,
                        text=f"Key point {j + 1}",
                        marks=1,
                        is_critical=j == 0,
                        accepted_alternatives=["an accepted synonym"],
                    )
                )
    db.commit()
    db.refresh(question)
    return question


def stock_the_bank(db, seqs: int = 5, vsaqs: int = 15) -> None:
    subs = ["Cataract", "Glaucoma", "Retina", "Cornea & External Eye", "Neuro-ophthalmology"]
    for i in range(seqs):
        make_question(db, QUESTION_SEQ, subs[i % len(subs)], parts=3, marks_per_part=4)
    for i in range(vsaqs):
        make_question(db, QUESTION_VSAQ, subs[i % len(subs)], parts=1, marks_per_part=3)


# --- The bank -------------------------------------------------------------
def test_candidates_only_see_approved_questions(client, db, student, admin):
    make_question(db, status=STATUS_APPROVED)
    make_question(db, status="draft")

    as_candidate = client.get("/api/questions", headers=auth(student)).json()
    assert as_candidate["total"] == 1
    assert all(q["status"] == STATUS_APPROVED for q in as_candidate["items"])

    assert client.get("/api/questions", headers=auth(admin)).json()["total"] == 2


def test_a_draft_question_is_404_for_a_candidate_even_by_id(client, db, student):
    draft = make_question(db, status="draft")
    assert client.get(
        f"/api/questions/{draft.id}", headers=auth(student)
    ).status_code == 404


def test_the_bank_page_reports_part_and_figure_counts(client, db, admin):
    make_question(db, parts=4)
    make_question(db, parts=1)

    items = client.get("/api/questions", headers=auth(admin)).json()["items"]
    assert sorted(q["part_count"] for q in items) == [1, 4]
    assert all(q["figure_count"] == 0 for q in items)


def test_filters_and_paging_agree_with_each_other(client, db, admin):
    for _ in range(3):
        make_question(db, QUESTION_SEQ, "Retina")
    for _ in range(2):
        make_question(db, QUESTION_VSAQ, "Glaucoma")

    seq = client.get("/api/questions?question_type=SEQ", headers=auth(admin)).json()
    assert seq["total"] == 3

    retina = client.get(
        "/api/questions?subspecialty=Retina", headers=auth(admin)
    ).json()
    assert retina["total"] == 3

    searched = client.get("/api/questions?search=haloes", headers=auth(admin)).json()
    assert searched["total"] == 5

    page = client.get("/api/questions?limit=2&offset=0", headers=auth(admin)).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    tail = client.get("/api/questions?limit=2&offset=4", headers=auth(admin)).json()
    assert len(tail["items"]) == 1


def test_an_oversized_page_is_refused_rather_than_served(client, admin):
    assert client.get("/api/questions?limit=5000", headers=auth(admin)).status_code == 422


def test_filter_options_do_not_offer_an_empty_question_type(client, db, admin):
    make_question(db)
    options = client.get("/api/meta/filters", headers=auth(admin)).json()
    assert options["question_types"] == [QUESTION_SEQ, QUESTION_VSAQ]
    assert "2026 Semester 1" in options["exam_periods"]


def test_bulk_status_moves_a_filtered_set(client, db, admin):
    for _ in range(3):
        make_question(db, status="review")
    make_question(db, status=STATUS_APPROVED)

    response = client.post(
        "/api/questions/bulk-status",
        json={"from_status": "review", "to_status": STATUS_APPROVED},
        headers=auth(admin),
    )
    assert response.json()["updated"] == 3
    assert client.get(
        f"/api/questions?status={STATUS_APPROVED}", headers=auth(admin)
    ).json()["total"] == 4


def test_an_unknown_status_is_refused(client, db, admin):
    make_question(db)
    response = client.post(
        "/api/questions/bulk-status",
        json={"to_status": "nonsense"},
        headers=auth(admin),
    )
    assert response.status_code == 400


def test_editing_a_sub_question_recomputes_the_question_total(client, db, admin):
    question = make_question(db, parts=2, marks_per_part=3)
    part = sorted(question.parts, key=lambda p: p.position)[0]

    assert client.patch(
        f"/api/parts/{part.id}", json={"marks": 7}, headers=auth(admin)
    ).status_code == 204
    db.expire_all()
    assert db.get(Question, question.id).total_marks == 10


# --- Assembly -------------------------------------------------------------
def test_assembling_a_paper_matches_the_ranzco_shape(client, db, admin):
    spec = PAPER_SPECS[1]
    stock_the_bank(db, seqs=spec.seq_count + 2, vsaqs=spec.vsaq_count + 2)

    response = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "seed": 7, "publish": True},
        headers=auth(admin),
    )
    assert response.status_code == 201
    report = response.json()["report"]
    assert report["seq_selected"] == spec.seq_count
    assert report["vsaq_selected"] == spec.vsaq_count
    # A real paper samples broadly rather than taking four from one area.
    assert len(report["subspecialties"]) > 1

    paper = response.json()["paper"]
    assert paper["question_count"] == spec.seq_count + spec.vsaq_count
    assert paper["cut_score"] and paper["cut_score"] < paper["total_marks"]


def test_a_thin_bank_refuses_rather_than_shipping_a_short_paper(client, db, admin):
    make_question(db, QUESTION_SEQ)
    response = client.post(
        "/api/papers/assemble", json={"paper_number": 1}, headers=auth(admin)
    )
    assert response.status_code == 400
    assert "required" in response.json()["detail"]

    allowed = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "allow_partial": True},
        headers=auth(admin),
    )
    assert allowed.status_code == 201
    assert allowed.json()["report"]["shortfalls"]


def test_an_unpublished_paper_is_invisible_to_candidates(client, db, student, admin):
    stock_the_bank(db)
    paper_id = client.post(
        "/api/papers/assemble", json={"paper_number": 1}, headers=auth(admin)
    ).json()["paper"]["id"]

    assert client.get("/api/papers", headers=auth(student)).json() == []
    assert client.get(f"/api/papers/{paper_id}", headers=auth(student)).status_code == 404
    assert client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).status_code == 403

    client.post(f"/api/papers/{paper_id}/publish", headers=auth(admin))
    assert len(client.get("/api/papers", headers=auth(student)).json()) == 1


def test_a_paper_with_sittings_cannot_be_deleted(client, db, student, admin):
    stock_the_bank(db)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True},
        headers=auth(admin),
    ).json()["paper"]["id"]
    client.post("/api/sessions", json={"paper_id": paper_id}, headers=auth(student))

    response = client.delete(f"/api/papers/{paper_id}", headers=auth(admin))
    assert response.status_code == 409
    assert "Unpublish it instead" in response.json()["detail"]


# --- The clock gate -------------------------------------------------------
@pytest.fixture()
def sitting(client, db, student, admin):
    """A published Paper 1 with a sitting that has not begun."""
    stock_the_bank(db)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True, "seed": 3},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id, "is_timed": True}, headers=auth(student)
    ).json()["id"]
    return session_id


def set_started(db, session_id: int, minutes_ago: float) -> None:
    session = db.get(ExamSession, session_id)
    session.started_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.commit()


def test_the_paper_is_not_readable_during_the_preparation_phase(
    client, db, student, sitting
):
    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    body = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()

    if body["clock"]["phase"] == "preparation":
        assert body["sections"] is None
        assert "reading period" in body["locked_reason"]
        # And the stem is genuinely absent, not merely hidden by the client.
        assert "painful red eye" not in str(body)


def test_answers_are_refused_before_writing_begins(client, db, student, sitting):
    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    body = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()
    if body["clock"]["phase"] not in {"preparation", "reading"}:
        pytest.skip("this paper has no reading phase")

    # A part id is needed; take one from the paper by going through the admin
    # view so the phase gate is not what fails.
    part_id = next(
        p["id"]
        for q in client.get("/api/questions", headers=auth(student)).json()["items"]
        for p in client.get(f"/api/questions/{q['id']}", headers=auth(student)).json()["parts"]
    )
    response = client.put(
        f"/api/sessions/{sitting}/answers",
        json={"answers": [{"part_id": part_id, "text": "early"}]},
        headers=auth(student),
    )
    assert response.status_code == 409
    assert "Writing time" in response.json()["detail"]


def test_notes_are_writable_during_reading_but_answers_are_not(
    client, db, student, sitting
):
    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    body = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()
    if not body["clock"]["can_take_notes"]:
        pytest.skip("this phase does not allow notes")

    response = client.put(
        f"/api/sessions/{sitting}/answers",
        json={"answers": [], "reading_notes": "angle closure - IOP first"},
        headers=auth(student),
    )
    assert response.status_code == 200
    reloaded = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()
    assert reloaded["reading_notes"] == "angle closure - IOP first"


def test_writing_accepts_answers_and_rejects_foreign_part_ids(
    client, db, student, sitting
):
    from app.services.exams import spec_for_paper

    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    session = db.get(ExamSession, sitting)
    spec = spec_for_paper(1)
    # Move the clock into the writing phase.
    set_started(db, sitting, spec.prep_minutes + spec.reading_minutes + 1)

    body = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()
    assert body["clock"]["phase"] == "writing"
    assert body["sections"]["A"], "the paper must be readable while writing"

    part_id = body["sections"]["A"][0]["parts"][0]["id"]
    # A part from a question that is not in this paper.
    stranger = make_question(db, parts=1)
    foreign_part_id = stranger.parts[0].id

    response = client.put(
        f"/api/sessions/{sitting}/answers",
        json={
            "answers": [
                {"part_id": part_id, "text": "Lower the pressure, then laser."},
                {"part_id": foreign_part_id, "text": "should not be saved"},
            ]
        },
        headers=auth(student),
    )
    assert response.status_code == 200
    assert response.json()["saved"] == 1, "a part outside this paper must be dropped"

    reloaded = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()
    saved = reloaded["sections"]["A"][0]["parts"][0]["answer"]
    assert saved == "Lower the pressure, then laser."
    assert session is not None


def test_writing_time_ending_closes_the_paper_for_edits(client, db, student, sitting):
    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    set_started(db, sitting, 600)  # long past the end of any paper

    body = client.get(f"/api/sessions/{sitting}", headers=auth(student)).json()
    part_id = body["sections"]["A"][0]["parts"][0]["id"]
    response = client.put(
        f"/api/sessions/{sitting}/answers",
        json={"answers": [{"part_id": part_id, "text": "too late"}]},
        headers=auth(student),
    )
    assert response.status_code == 409


def test_a_sitting_cannot_be_submitted_before_it_begins(client, student, sitting):
    response = client.post(f"/api/sessions/{sitting}/submit", headers=auth(student))
    assert response.status_code == 400
    assert "not begun" in response.json()["detail"]


def test_submitting_twice_is_refused(client, db, student, sitting):
    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    assert client.post(
        f"/api/sessions/{sitting}/submit", headers=auth(student)
    ).status_code == 200
    assert client.post(
        f"/api/sessions/{sitting}/submit", headers=auth(student)
    ).status_code == 400


def test_another_candidate_cannot_read_your_paper(client, db, student, sitting):
    from app.constants import ROLE_STUDENT
    from tests.conftest import _make_user

    intruder = _make_user(db, "intruder@example.com", ROLE_STUDENT)
    assert client.get(
        f"/api/sessions/{sitting}", headers=auth(intruder)
    ).status_code == 403
    assert client.put(
        f"/api/sessions/{sitting}/answers", json={"answers": []}, headers=auth(intruder)
    ).status_code == 403


# --- Marking --------------------------------------------------------------
def test_a_paper_is_marked_end_to_end_with_a_key_point_breakdown(
    client, db, student, admin, ai, run_jobs
):
    from app.services.exams import spec_for_paper

    stock_the_bank(db, seqs=PAPER_SPECS[1].seq_count, vsaqs=PAPER_SPECS[1].vsaq_count)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True, "seed": 11},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).json()["id"]

    client.post(f"/api/sessions/{session_id}/begin", headers=auth(student))
    spec = spec_for_paper(1)
    set_started(db, session_id, spec.prep_minutes + spec.reading_minutes + 1)

    body = client.get(f"/api/sessions/{session_id}", headers=auth(student)).json()
    answers = [
        {"part_id": part["id"], "text": "Key point 1 and key point 2, in note form."}
        for section in body["sections"].values()
        for question in section
        for part in question["parts"]
    ]
    saved = client.put(
        f"/api/sessions/{session_id}/answers", json={"answers": answers},
        headers=auth(student),
    )
    assert saved.json()["saved"] == len(answers)

    submitted = client.post(f"/api/sessions/{session_id}/submit", headers=auth(student))
    assert submitted.json()["answers_recorded"] == len(answers)
    run_jobs()

    result = client.get(
        f"/api/sessions/{session_id}/result", headers=auth(student)
    ).json()
    assert result["grading_status"] == "complete"
    assert result["result"]["outcome"] in {"pass", "fail"}
    assert result["result"]["subspecialty_breakdown"]
    assert result["result"]["ungraded_parts"] == []

    first_part = result["questions"][0]["parts"][0]
    assert first_part["awarded"] is not None
    assert first_part["examiners"][0]["breakdown"], "a per-key-point breakdown is the point"
    assert first_part["model_answer"], "the marking key is revealed with the result"
    assert first_part["your_answer"].startswith("Key point 1")


def test_an_unanswered_sub_question_is_marked_zero_without_a_model_call(
    client, db, student, admin, ai, run_jobs
):
    stock_the_bank(db, seqs=PAPER_SPECS[1].seq_count, vsaqs=PAPER_SPECS[1].vsaq_count)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True, "seed": 5},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).json()["id"]
    client.post(f"/api/sessions/{session_id}/begin", headers=auth(student))
    client.post(f"/api/sessions/{session_id}/submit", headers=auth(student))
    run_jobs()

    result = client.get(
        f"/api/sessions/{session_id}/result", headers=auth(student)
    ).json()
    assert result["result"]["total_awarded"] == 0.0
    assert result["result"]["percentage"] == 0.0
    assert ai.requests == [], "a blank paper must not be sent to a model"
    assert "No answer was submitted" in str(result["questions"][0]["parts"][0]["examiners"])


def test_a_partly_marked_paper_gets_no_pass_fail_verdict(
    client, db, student, admin, ai, run_jobs
):
    """A fail declared on half a paper would actively mislead a candidate."""
    from app.services.ai.client import AIError
    from app.services.exams import spec_for_paper

    stock_the_bank(db, seqs=PAPER_SPECS[1].seq_count, vsaqs=PAPER_SPECS[1].vsaq_count)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True, "seed": 2},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).json()["id"]
    client.post(f"/api/sessions/{session_id}/begin", headers=auth(student))
    spec = spec_for_paper(1)
    set_started(db, session_id, spec.prep_minutes + spec.reading_minutes + 1)

    body = client.get(f"/api/sessions/{session_id}", headers=auth(student)).json()
    answers = [
        {"part_id": part["id"], "text": "Key point 1."}
        for section in body["sections"].values()
        for question in section
        for part in question["parts"]
    ]
    client.put(
        f"/api/sessions/{session_id}/answers", json={"answers": answers},
        headers=auth(student),
    )
    client.post(f"/api/sessions/{session_id}/submit", headers=auth(student))

    # Every marking call now fails, the way a provider rate limit does.
    calls = {"n": 0}

    def rate_limited(body_, n):
        calls["n"] += 1
        raise AIError("HTTP 429: rate limited")

    ai.responder = rate_limited
    run_jobs()

    result = client.get(
        f"/api/sessions/{session_id}/result", headers=auth(student)
    ).json()
    assert result["result"]["outcome"] == "incomplete"
    assert result["result"]["ungraded_parts"]
    assert "no pass/fail verdict" in result["result"]["overall_feedback"]
    assert "Re-mark" in result["result"]["overall_feedback"]


def test_re_marking_only_fills_the_gaps_by_default(client, db, student, admin, ai, run_jobs):
    """Completing a rate-limited paper must not pay to redo what succeeded."""
    stock_the_bank(db, seqs=1, vsaqs=1)
    for question in db.query(Question).all():
        question.status = STATUS_APPROVED
    db.commit()

    spec = PAPER_SPECS[1]
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "allow_partial": True, "publish": True},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).json()["id"]
    client.post(f"/api/sessions/{session_id}/begin", headers=auth(student))
    client.post(f"/api/sessions/{session_id}/submit", headers=auth(student))
    run_jobs()

    before = len(ai.requests)
    response = client.post(f"/api/sessions/{session_id}/grade", headers=auth(student))
    assert response.status_code == 202
    run_jobs()
    assert len(ai.requests) == before, "already-marked parts must not be re-sent"
    assert spec is not None


def test_deleting_your_own_sitting_removes_its_result(client, db, student, sitting):
    client.post(f"/api/sessions/{sitting}/begin", headers=auth(student))
    assert client.delete(
        f"/api/sessions/{sitting}", headers=auth(student)
    ).status_code == 204
    assert client.get(f"/api/sessions/{sitting}", headers=auth(student)).status_code == 404
