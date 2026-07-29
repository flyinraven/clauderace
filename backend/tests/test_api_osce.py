"""A whole OSCE station, from browsing to a marked result.

Covers the sequence a candidate actually performs - list, start, begin, record
each answer, review the transcripts, submit, read the result - plus the things
that must NOT happen along the way: no diagnosis before the answers, no marking
of a station someone else is sitting, no recording after the clock expires.
"""

from __future__ import annotations

import io

from app.models import AudioClip, OsceResponse, OsceSession, OsceStation
from tests.conftest import auth

STATION_PROMPTS = [
    {
        "label": "A",
        "text": "Please examine the anterior segment of the left eye.",
        "seconds": 270,
        "rubric": [
            {"text": "Describes the central corneal opacity", "marks": 6, "is_critical": True},
            {"text": "Notes the absence of hypopyon", "marks": 4, "is_critical": False},
        ],
    },
    {
        "label": "B",
        "text": "What are the risk factors for this condition? Name 4.",
        "seconds": 270,
        "rubric": [{"text": "Names four risk factors", "marks": 10, "is_critical": False}],
    },
]


def make_station(db, **overrides) -> OsceStation:
    station = OsceStation(
        station_number=1,
        title="Herpetic keratitis",
        subspecialty="Cornea & External Eye",
        case_summary="A 68-year-old with a painful red eye and reduced vision.",
        patient_history="Recurrent episodes over two years.",
        patient_demographic="An elderly woman",
        findings="VA 6/24, IOP 16, dense central stromal opacity with neovascularisation.",
        findings_given="Visual acuity 6/24 left. Intraocular pressure 16 mmHg.",
        findings_elicited="Dense central stromal opacity with deep neovascularisation.",
        findings_split_status="complete",
        diagnosis="Herpes simplex stromal keratitis",
        common_mistakes=["Missed the neovascularisation"],
        total_marks=20,
        source="past_paper",
        exam_period="2026 Semester 1",
        status="approved",
        prompts_status="complete",
        prompts=STATION_PROMPTS,
    )
    for key, value in overrides.items():
        setattr(station, key, value)
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


def audio(size: int = 40_000) -> bytes:
    """Bytes large enough to clear the near-silence guard."""
    return b"\x00\x01\x02\x03" * (size // 4)


# --- Browsing -------------------------------------------------------------
def test_station_list_hides_the_case_summary_from_candidates(client, db, student, admin):
    make_station(db)

    as_candidate = client.get("/api/osce/stations", headers=auth(student)).json()[0]
    assert as_candidate["case_summary"] is None, "the case summary names the diagnosis"

    as_admin = client.get("/api/osce/stations", headers=auth(admin)).json()[0]
    assert as_admin["case_summary"].startswith("A 68-year-old")


def test_station_list_reports_this_candidates_attempts_only(client, db, student, admin):
    station = make_station(db)
    db.add(OsceSession(user_id=admin.id, station_id=station.id))
    db.commit()

    mine = client.get("/api/osce/stations", headers=auth(student)).json()[0]
    assert mine["attempted"] is False
    assert mine["attempt_count"] == 0

    theirs = client.get("/api/osce/stations", headers=auth(admin)).json()[0]
    assert theirs["attempted"] is True
    assert theirs["attempt_count"] == 1


def test_a_station_without_prompts_cannot_be_sat(client, db, student):
    station = make_station(db, prompts=[], prompts_status="none")
    response = client.post(
        "/api/osce/sittings",
        json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    )
    assert response.status_code == 400
    assert "no examiner questions" in response.json()["detail"]


# --- Sitting --------------------------------------------------------------
def test_the_sitting_withholds_everything_the_candidate_must_elicit(client, db, student):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))

    body = client.get(f"/api/osce/sittings/{sitting_id}", headers=auth(student)).json()
    served = str(body)

    assert body["station"]["patient_demographic"] == "An elderly woman"
    assert body["station"]["findings_given"].startswith("Visual acuity")
    for secret in (
        station.diagnosis,
        station.findings_elicited,
        station.case_summary,
        station.patient_history,
    ):
        assert secret not in served, f"the sitting leaked: {secret!r}"
    # The rubric is the answer key for every question.
    assert "Describes the central corneal opacity" not in served


def test_unsplit_findings_are_withheld_rather_than_risked(client, db, student):
    """Until given/elicited are separated, showing 'findings' leaks the signs."""
    station = make_station(db, findings_split_status="none")
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]

    body = client.get(f"/api/osce/sittings/{sitting_id}", headers=auth(student)).json()
    assert body["station"]["findings_given"] is None
    assert body["station"]["findings_pending_split"] is True


def test_beginning_twice_is_refused(client, db, student):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    assert client.post(
        f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student)
    ).status_code == 200
    again = client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))
    assert again.status_code == 400


def test_another_candidate_cannot_touch_your_sitting(client, db, student, admin):
    """Admins may inspect; another candidate may not."""
    from app.constants import ROLE_STUDENT
    from tests.conftest import _make_user

    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]

    intruder = _make_user(db, "other@example.com", ROLE_STUDENT)
    assert client.get(
        f"/api/osce/sittings/{sitting_id}", headers=auth(intruder)
    ).status_code == 403
    assert client.post(
        f"/api/osce/sittings/{sitting_id}/submit", headers=auth(intruder)
    ).status_code == 403
    assert client.get(
        f"/api/osce/sittings/{sitting_id}", headers=auth(admin)
    ).status_code == 200


# --- Recording ------------------------------------------------------------
def upload(client, user, sitting_id, label, index, data=None, content_type="audio/webm"):
    return client.post(
        f"/api/osce/sittings/{sitting_id}/answers",
        headers=auth(user),
        files={
            "audio": (
                f"answer-{label}.webm",
                io.BytesIO(audio() if data is None else data),
                content_type,
            )
        },
        data={"prompt_label": label, "prompt_index": str(index), "duration_ms": "45000"},
    )


def test_an_answer_is_stored_and_queued_for_transcription(client, db, student):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))

    response = upload(client, student, sitting_id, "A", 0)
    assert response.status_code == 201
    assert response.json()["job_id"]

    stored = db.execute(
        OsceResponse.__table__.select().where(OsceResponse.session_id == sitting_id)
    ).all()
    assert len(stored) == 1

    body = client.get(f"/api/osce/sittings/{sitting_id}", headers=auth(student)).json()
    assert body["prompts"][0]["transcription_status"] == "pending"
    assert body["current_prompt_index"] == 1


def test_re_recording_the_same_question_replaces_the_answer(client, db, student):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))

    upload(client, student, sitting_id, "A", 0)
    upload(client, student, sitting_id, "A", 0, data=audio(60_000))

    rows = db.execute(
        OsceResponse.__table__.select().where(OsceResponse.session_id == sitting_id)
    ).all()
    assert len(rows) == 1, "a re-record must not create a second answer"


def test_an_empty_or_oversized_recording_is_refused(client, db, student):
    from app.api.osce import MAX_AUDIO_BYTES

    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))

    assert upload(client, student, sitting_id, "A", 0, data=b"").status_code == 400
    too_big = upload(
        client, student, sitting_id, "A", 0, data=b"x" * (MAX_AUDIO_BYTES + 1)
    )
    assert too_big.status_code == 413
    wrong_type = upload(
        client, student, sitting_id, "A", 0, content_type="application/pdf"
    )
    assert wrong_type.status_code == 400


def test_no_recording_is_accepted_once_the_clock_has_run_out(client, db, student):
    from datetime import datetime, timedelta, timezone

    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]

    sitting = db.get(OsceSession, sitting_id)
    sitting.started_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.commit()

    response = upload(client, student, sitting_id, "A", 0)
    assert response.status_code == 409
    assert "clock" in response.json()["detail"]


def test_an_untimed_station_never_expires(client, db, student):
    from datetime import datetime, timedelta, timezone

    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": False},
        headers=auth(student),
    ).json()["id"]
    sitting = db.get(OsceSession, sitting_id)
    sitting.started_at = datetime.now(timezone.utc) - timedelta(hours=4)
    db.commit()

    assert upload(client, student, sitting_id, "A", 0).status_code == 201


# --- Transcription, review, marking --------------------------------------
def test_a_station_runs_through_to_a_marked_result(client, db, student, ai, run_jobs):
    """The full path: two answers, transcription, submission, marking."""
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))

    # Transcription goes through Google's native endpoint, which is a separate
    # HTTP path from the chat completions the fake provider serves.
    def fake_transcribe(db_, data, content_type, store=None):
        return "There is a dense central corneal opacity with neovascularisation."

    import app.services.osce.transcribe as transcribe_module
    import app.services.osce.transcribe_job as job_module

    original = job_module.transcribe_response

    def patched(db_, response):
        response.transcript = fake_transcribe(db_, b"", "audio/webm")
        response.transcription_status = "complete"
        db_.commit()
        return response.transcript

    job_module.transcribe_response = patched
    try:
        upload(client, student, sitting_id, "A", 0)
        upload(client, student, sitting_id, "B", 1)
        run_jobs()

        body = client.get(f"/api/osce/sittings/{sitting_id}", headers=auth(student)).json()
        assert [p["transcription_status"] for p in body["prompts"]] == ["complete", "complete"]

        # Correct a mis-heard word before marking.
        fixed = client.put(
            f"/api/osce/sittings/{sitting_id}/answers/A/transcript",
            json={"transcript": "Dense central corneal opacity, no hypopyon."},
            headers=auth(student),
        )
        assert fixed.status_code == 200

        submitted = client.post(
            f"/api/osce/sittings/{sitting_id}/submit", headers=auth(student)
        )
        assert submitted.status_code == 200
        assert submitted.json()["grading_job_id"]
        run_jobs()
    finally:
        job_module.transcribe_response = original
        assert transcribe_module is not None

    result = client.get(
        f"/api/osce/sittings/{sitting_id}/result", headers=auth(student)
    ).json()
    assert result["grading_status"] == "complete"
    assert result["result"]["outcome"] in {"pass", "fail"}
    assert result["result"]["total_available"] == 20
    # The edited transcript is what was marked.
    assert result["prompts"][0]["transcript"] == "Dense central corneal opacity, no hypopyon."
    # And now the diagnosis is safe to show.
    assert result["station"]["diagnosis"] == "Herpes simplex stromal keratitis"
    assert result["station"]["findings_elicited"]


def test_marking_never_awards_more_than_a_rubric_point_is_worth(client, db, student, ai):
    """A model that returns 99 marks for a 6-mark point must be clamped."""
    import json

    from app.services.ai import AIClient
    from app.services.osce.circuit import grade_prompt

    station = make_station(db)
    sitting = OsceSession(user_id=student.id, station_id=station.id)
    db.add(sitting)
    db.commit()

    ai.responder = lambda body, n: json.dumps(
        {
            "breakdown": [
                {"index": 0, "awarded": 99, "comment": "over"},
                {"index": 1, "awarded": -5, "comment": "under"},
                {"index": 7, "awarded": 3, "comment": "no such point"},
            ],
            "awarded_total": 99,
            "feedback": "ok",
        }
    )

    grade = grade_prompt(
        db, AIClient(db), sitting, station, STATION_PROMPTS[0], "an answer", 1
    )
    assert grade.awarded_marks == 6.0, "clamped to the point's own 6 marks"
    assert grade.available_marks == 10.0
    assert [b["index"] for b in grade.breakdown] == [0, 1]


def test_a_transcript_cannot_be_edited_after_marking(client, db, student, ai, run_jobs):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))
    client.post(f"/api/osce/sittings/{sitting_id}/submit", headers=auth(student))
    run_jobs()

    late = client.put(
        f"/api/osce/sittings/{sitting_id}/answers/A/transcript",
        json={"transcript": "a better answer, after the fact"},
        headers=auth(student),
    )
    assert late.status_code == 409


def test_a_station_with_nothing_recorded_scores_zero_without_a_model_call(
    client, db, student, ai, run_jobs
):
    station = make_station(db)
    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    client.post(f"/api/osce/sittings/{sitting_id}/begin", headers=auth(student))
    client.post(f"/api/osce/sittings/{sitting_id}/submit", headers=auth(student))
    run_jobs()

    result = client.get(
        f"/api/osce/sittings/{sitting_id}/result", headers=auth(student)
    ).json()
    assert result["result"]["total_awarded"] == 0.0
    assert result["result"]["outcome"] == "fail"
    assert ai.requests == [], "silence must not be sent to a model to be marked"


# --- Attempts and circuits ------------------------------------------------
def test_clearing_attempts_only_clears_your_own(client, db, student, admin):
    station = make_station(db)
    for user in (student, admin):
        client.post(
            "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
            headers=auth(user),
        )

    cleared = client.delete(
        f"/api/osce/stations/{station.id}/attempts", headers=auth(student)
    )
    assert cleared.json() == {"cleared": 1}
    assert client.get("/api/osce/stations", headers=auth(admin)).json()[0]["attempted"] is True


def test_clearing_every_attempt_wipes_only_this_candidates(client, db, student, admin):
    for i in range(3):
        station = make_station(db, station_number=i + 1)
        client.post(
            "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
            headers=auth(student),
        )
        if i == 0:
            client.post(
                "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
                headers=auth(admin),
            )

    assert client.delete("/api/osce/attempts", headers=auth(student)).json() == {"cleared": 3}
    remaining = db.execute(OsceSession.__table__.select()).all()
    assert len(remaining) == 1


def test_a_circuit_is_built_and_its_progress_reported(client, db, student):
    subs = ["Cataract", "Glaucoma", "Retina", "Cornea & External Eye"]
    for i, sub in enumerate(subs):
        make_station(db, station_number=i + 1, subspecialty=sub)

    created = client.post(
        "/api/osce/circuits", json={"station_count": 4}, headers=auth(student)
    )
    assert created.status_code == 201
    assert len(created.json()["station_ids"]) == 4
    assert created.json()["progress"]["completed"] == 0

    listed = client.get("/api/osce/circuits", headers=auth(student)).json()
    assert len(listed) == 1
    assert listed[0]["progress"]["stations"] == 4


def test_a_circuit_with_no_unsat_stations_is_refused_with_a_reason(client, db, student):
    station = make_station(db)
    client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    )
    response = client.post(
        "/api/osce/circuits", json={"station_count": 1}, headers=auth(student)
    )
    assert response.status_code == 400
    assert "Clear an attempt" in response.json()["detail"]


def test_circuits_are_private_to_the_candidate(client, db, student, admin):
    make_station(db)
    client.post("/api/osce/circuits", json={"station_count": 1}, headers=auth(student))
    from app.constants import ROLE_STUDENT
    from tests.conftest import _make_user

    other = _make_user(db, "third@example.com", ROLE_STUDENT)
    assert client.get("/api/osce/circuits", headers=auth(other)).json() == []


# --- Audio retention ------------------------------------------------------
def test_the_recording_is_released_once_transcribed(client, db, student, ai):
    from app.services.osce.transcribe import transcribe_response

    station = make_station(db)
    sitting = OsceSession(user_id=student.id, station_id=station.id)
    db.add(sitting)
    db.flush()
    clip = AudioClip(sha256="a" * 64, content_type="audio/webm", data=audio(),
                     size_bytes=40_000, duration_ms=20_000)
    db.add(clip)
    db.flush()
    response = OsceResponse(
        session_id=sitting.id, prompt_label="A", prompt_index=0, audio_clip_id=clip.id
    )
    db.add(response)
    db.commit()

    import app.services.osce.transcribe as module

    module.transcribe_audio = lambda *a, **k: "the candidate said this"
    transcribe_response(db, response)

    assert response.transcript == "the candidate said this"
    assert clip.data is None, "audio is dropped once the transcript exists"
    assert clip.is_discarded is True


def test_sourcing_can_be_limited_to_named_stations(client, db, admin, monkeypatch):
    """Sourcing all 81 at once is a long run of paid calls on one free instance.

    Naming the stations lets it be done in batches whose results can be looked
    at before spending on the next.
    """
    from app.models import OsceStation

    ids = []
    for n in range(3):
        st = OsceStation(station_number=n + 1, total_marks=20, source="past_paper",
                         status="review", subspecialty="Glaucoma")
        db.add(st)
        db.flush()
        ids.append(st.id)
    db.commit()

    response = client.post(
        "/api/osce/stations/source-images",
        json={"station_ids": ids[:2]},
        headers=auth(admin),
    )
    assert response.status_code == 202
    assert response.json()["station_count"] == 2

    from app.models import Job
    job = db.get(Job, response.json()["job_id"])
    assert job.payload["station_ids"] == ids[:2], "only the named stations"


def test_a_batch_can_be_capped_without_naming_them(client, db, admin):
    from app.models import OsceStation

    for n in range(5):
        db.add(OsceStation(station_number=n + 1, total_marks=20, source="past_paper",
                           status="review", subspecialty="Glaucoma"))
    db.commit()

    response = client.post(
        "/api/osce/stations/source-images", json={"limit": 2}, headers=auth(admin)
    )
    assert response.status_code == 202
    assert response.json()["station_count"] == 2


def test_sourcing_with_no_body_still_does_everything_that_needs_it(client, db, admin):
    """The button that sends no body must keep working."""
    from app.models import OsceStation

    db.add(OsceStation(station_number=1, total_marks=20, source="past_paper",
                       status="review", subspecialty="Glaucoma"))
    db.commit()

    response = client.post("/api/osce/stations/source-images", headers=auth(admin))
    assert response.status_code == 202
    assert response.json()["station_count"] == 1
