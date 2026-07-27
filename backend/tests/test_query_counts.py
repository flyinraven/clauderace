"""Query counts for the pages that grow with content.

The API runs on Render and the database on SiteGround, so every query carries
real network latency. A page whose query count grows with the number of rows on
it is the failure mode that matters here: it works on a laptop against SQLite
and takes fifteen seconds in production.

These are upper bounds, not exact figures - the point is that they do not scale
with the amount of content, so each test compares a small page against a much
larger one and asserts the count barely moves.
"""

from __future__ import annotations

from sqlalchemy import event

from app.constants import PAPER_SPECS
from tests.conftest import auth
from tests.test_api_exams import make_question, set_started, stock_the_bank
from tests.test_api_osce import STATION_PROMPTS, make_station


class QueryCounter:
    """Counts statements issued on an engine while active."""

    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []

    def _record(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._record)

    def __len__(self) -> int:
        return len(self.statements)


def test_the_question_bank_page_does_not_query_per_row(client, db, engine, admin):
    headers = auth(admin)
    for _ in range(2):
        make_question(db, parts=3)
    client.get("/api/questions", headers=headers)  # warm any one-off reads

    with QueryCounter(engine) as small:
        assert client.get("/api/questions", headers=headers).status_code == 200

    for _ in range(20):
        make_question(db, parts=3)
    with QueryCounter(engine) as large:
        page = client.get("/api/questions", headers=headers)
    assert page.json()["total"] == 22

    assert len(large) == len(small), (
        f"the bank page cost {len(small)} queries for 2 questions and "
        f"{len(large)} for 22 - it is querying per row"
    )


def test_opening_a_paper_does_not_query_per_question(client, db, engine, student, admin):
    spec = PAPER_SPECS[1]
    stock_the_bank(db, seqs=spec.seq_count, vsaqs=spec.vsaq_count)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True, "seed": 4},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).json()["id"]
    client.post(f"/api/sessions/{session_id}/begin", headers=auth(student))
    set_started(db, session_id, spec.prep_minutes + spec.reading_minutes + 1)

    client.get(f"/api/sessions/{session_id}", headers=auth(student))
    with QueryCounter(engine) as counter:
        body = client.get(f"/api/sessions/{session_id}", headers=auth(student))
    assert body.json()["sections"]["A"]

    questions = spec.seq_count + spec.vsaq_count
    assert len(counter) < questions, (
        f"{len(counter)} queries to open a {questions}-question paper - "
        f"the questions, their parts or their figures are loading one at a time"
    )


def test_a_marked_result_does_not_query_per_sub_question(
    client, db, engine, student, admin, ai, run_jobs
):
    spec = PAPER_SPECS[1]
    stock_the_bank(db, seqs=spec.seq_count, vsaqs=spec.vsaq_count)
    paper_id = client.post(
        "/api/papers/assemble",
        json={"paper_number": 1, "publish": True, "seed": 9},
        headers=auth(admin),
    ).json()["paper"]["id"]
    session_id = client.post(
        "/api/sessions", json={"paper_id": paper_id}, headers=auth(student)
    ).json()["id"]
    client.post(f"/api/sessions/{session_id}/begin", headers=auth(student))
    set_started(db, session_id, spec.prep_minutes + spec.reading_minutes + 1)

    body = client.get(f"/api/sessions/{session_id}", headers=auth(student)).json()
    parts = [
        part["id"]
        for section in body["sections"].values()
        for question in section
        for part in question["parts"]
    ]
    client.put(
        f"/api/sessions/{session_id}/answers",
        json={"answers": [{"part_id": p, "text": "Key point 1."} for p in parts]},
        headers=auth(student),
    )
    client.post(f"/api/sessions/{session_id}/submit", headers=auth(student))
    run_jobs()

    client.get(f"/api/sessions/{session_id}/result", headers=auth(student))
    with QueryCounter(engine) as counter:
        result = client.get(f"/api/sessions/{session_id}/result", headers=auth(student))
    assert result.json()["grading_status"] == "complete"

    # Before the fix this page cost two queries for every sub-question on top of
    # the per-question reads.
    assert len(counter) < len(parts), (
        f"{len(counter)} queries for a result with {len(parts)} sub-questions"
    )


def test_an_osce_result_does_not_query_per_question(
    client, db, engine, student, ai, run_jobs
):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))
    client.post(f"/api/osce/sittings/{sitting_id}/submit", headers=auth(student))
    run_jobs()

    client.get(f"/api/osce/sittings/{sitting_id}/result", headers=auth(student))
    with QueryCounter(engine) as counter:
        result = client.get(
            f"/api/osce/sittings/{sitting_id}/result", headers=auth(student)
        )
    assert result.status_code == 200
    # Sitting, station, result, responses, grades, plus the user lookup.
    assert len(counter) <= 8, f"{len(counter)} queries for one station's result"


def test_the_osce_page_does_not_query_per_station_in_a_circuit(
    client, db, engine, student
):
    subs = ["Cataract", "Glaucoma", "Retina", "Cornea & External Eye",
            "Neuro-ophthalmology", "Oculoplastics & Orbit"]
    for i, sub in enumerate(subs):
        make_station(db, station_number=i + 1, subspecialty=sub)

    circuit = client.post(
        "/api/osce/circuits", json={"station_count": 6}, headers=auth(student)
    ).json()
    for station_id in circuit["station_ids"]:
        sitting_id = client.post(
            "/api/osce/sittings",
            json={"station_id": station_id, "circuit_id": circuit["id"]},
            headers=auth(student),
        ).json()["id"]
        client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))
        client.post(f"/api/osce/sittings/{sitting_id}/submit", headers=auth(student))

    client.get("/api/osce/circuits", headers=auth(student))
    with QueryCounter(engine) as counter:
        listed = client.get("/api/osce/circuits", headers=auth(student))
    assert listed.json()[0]["progress"]["completed"] == 6

    # User, circuits, then per circuit its sittings and their results.
    assert len(counter) <= 6, (
        f"{len(counter)} queries to list one 6-station circuit - progress is "
        f"reading results one sitting at a time"
    )


def test_the_station_list_cost_is_flat_in_the_number_of_stations(
    client, db, engine, student
):
    for i in range(3):
        make_station(db, station_number=i + 1, prompts=STATION_PROMPTS)
    client.get("/api/osce/stations", headers=auth(student))
    with QueryCounter(engine) as small:
        client.get("/api/osce/stations", headers=auth(student))

    for i in range(30):
        make_station(db, station_number=i + 10, prompts=STATION_PROMPTS)
    with QueryCounter(engine) as large:
        response = client.get("/api/osce/stations", headers=auth(student))
    assert len(response.json()) == 33
    assert len(large) == len(small)
