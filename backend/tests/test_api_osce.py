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


def test_a_sweep_spends_only_on_what_is_missing_but_a_named_station_is_redone(
    client, db, admin
):
    """Most of a batch is stations whose own image is fine.

    They are in it because a question further down wants an MRI, and re-sourcing
    the opening photograph costs a search and a vision call to arrive back where
    it started. Naming a station is the opposite intent: redo it.
    """
    from app.models import Job, OsceStation

    station = OsceStation(station_number=1, total_marks=20, source="past_paper",
                          status="review", subspecialty="Glaucoma")
    db.add(station)
    db.commit()

    sweep = client.post("/api/osce/stations/source-images", headers=auth(admin))
    assert db.get(Job, sweep.json()["job_id"]).payload["only_missing"] is True

    named = client.post(
        "/api/osce/stations/source-images",
        json={"station_ids": [station.id]},
        headers=auth(admin),
    )
    assert db.get(Job, named.json()["job_id"]).payload["only_missing"] is False

    forced = client.post(
        "/api/osce/stations/source-images",
        json={"only_missing": False},
        headers=auth(admin),
    )
    assert db.get(Job, forced.json()["job_id"]).payload["only_missing"] is False


def test_only_a_confident_approved_image_is_left_alone_by_a_sweep(db):
    """The same test the audit applies, so the two cannot disagree."""
    from app.models import Image, OsceFigure
    from app.services.osce.station_images import opening_image_is_settled

    station = make_station(db)
    assert not opening_image_is_settled(station), "no figure at all"

    image = Image(sha256="a" * 64, content_type="image/jpeg", data=b"jpeg",
                  size_bytes=4, origin="web", source_url="https://example/x.jpg")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        is_approved=True, verification_status="faithful",
                        match_confidence=0.9)
    db.add(figure)
    db.commit()
    db.refresh(station)
    assert opening_image_is_settled(station)

    # A picture of the right disease and the wrong patient is worth one more
    # search, and so is one that only scraped past the attachment gate.
    figure.verification_status = "representative"
    db.commit()
    assert not opening_image_is_settled(station)

    figure.verification_status = "faithful"
    figure.match_confidence = 0.72
    db.commit()
    assert not opening_image_is_settled(station)

    # An unapproved image shows the candidate nothing, so it is not settled.
    figure.match_confidence = 0.9
    figure.is_approved = False
    db.commit()
    assert not opening_image_is_settled(station)

    # Sourced before the tier was named, or before the score was recorded.
    figure.is_approved = True
    figure.verification_status = "verified"
    figure.match_confidence = None
    db.commit()
    assert opening_image_is_settled(station)


def test_a_motility_station_is_re_sourced_however_good_its_single_photograph(db):
    """One primary-position photograph passes every other test and still fails.

    The station this comes from had a confident, approved, faithful frontal
    photograph, and asked the candidate to examine the right eye's motility. A
    sweep would have skipped it for ever.
    """
    from app.models import Image, OsceFigure
    from app.services.imagesearch.relevance import GAZE_PHRASE
    from app.services.osce.station_images import opening_image_is_settled

    station = make_station(db, prompts=[{
        "label": "A",
        "text": "Please examine the ocular motility of the right eye.",
        "seconds": 180,
        "rubric": [
            {"text": "Identify deficits in right MR, SR, IR."},
            {"text": "Identify right LR deficit."},
        ],
    }])
    image = Image(sha256="b" * 64, content_type="image/jpeg", data=b"jpeg",
                  size_bytes=4, origin="web", source_url="https://example/y.jpg")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        is_approved=True, verification_status="faithful",
                        match_confidence=0.95,
                        wanted_description="external photograph of the right eye")
    db.add(figure)
    db.commit()
    db.refresh(station)
    assert not opening_image_is_settled(station)

    # Asking for the montage is not the same as having one. This assertion used
    # to be the other way round, keyed on wanted_description, and five stations
    # still showing a single position went quiet the moment they had been
    # searched - the re-source writes the phrase whatever it finds.
    figure.wanted_description = f"{GAZE_PHRASE} showing right LR deficit"
    db.commit()
    db.refresh(station)
    assert not opening_image_is_settled(station)

    # What settles it is a montage actually being there.
    figure.verification_notes = "A nine-panel montage of the eyes in nine positions of gaze."
    db.commit()
    db.refresh(station)
    assert opening_image_is_settled(station)


def test_a_montage_already_found_is_not_paid_for_twice(db):
    """Five of the fourteen stations first flagged already had a nine-panel series.

    The searcher stumbled into them before the requirement was named, so nothing
    in the recorded wanted_description says so. What does is the description the
    vision model wrote when it attached the image, and reading it is free.
    """
    from app.models import Image, OsceFigure
    from app.services.osce.station_images import wants_gaze_montage

    station = make_station(db, prompts=[{
        "label": "A",
        "text": "Examine the ocular motility and describe the findings.",
        "seconds": 180,
        "rubric": [{"text": "Identifies limitation of elevation of the right eye"}],
    }])
    image = Image(sha256="c" * 64, content_type="image/jpeg", data=b"jpeg",
                  size_bytes=4, origin="web", source_url="https://example/z.jpg")
    db.add(image)
    db.flush()
    figure = OsceFigure(
        station_id=station.id, position=0, image_id=image.id, is_approved=True,
        verification_status="faithful", match_confidence=0.9,
        wanted_description="Marked limitation of elevation of the right eye",
        verification_notes="A nine-panel montage of the eyes in different gaze positions.",
        caption="External photographs of the eyes in nine positions of gaze.",
    )
    db.add(figure)
    db.commit()
    db.refresh(station)
    assert not wants_gaze_montage(station, figure)

    # Panels of something else are not positions of gaze.
    figure.verification_notes = "A multi-panel photograph of a child's eyes without and with glasses."
    figure.caption = "External photograph of a child's eyes, without and with glasses."
    db.commit()
    assert wants_gaze_montage(station, figure)

    # And one photograph that merely says which position it is in is the very
    # thing being caught.
    figure.verification_notes = "A frontal photograph of the eyes in primary gaze."
    figure.caption = "Frontal photograph of the face in primary position"
    db.commit()
    assert wants_gaze_montage(station, figure)


def test_deleting_a_circuit_keeps_the_sittings_it_ran(client, db, student):
    """A circuit is a plan for a day, not the record of the work done in it.

    The relationship cascades delete-orphan, so deleting one with its sittings
    attached would take every recorded answer and mark with it - which is not
    what anyone tidying a list is asking for.
    """
    from app.models import OsceCircuit, OsceSession

    station = make_station(db)
    circuit = OsceCircuit(
        user_id=student.id, title="Tuesday circuit", station_ids=[station.id],
    )
    db.add(circuit)
    db.commit()
    circuit_id = circuit.id

    sitting_id = client.post(
        "/api/osce/sittings",
        json={"station_id": station.id, "circuit_id": circuit_id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]

    response = client.delete(f"/api/osce/circuits/{circuit_id}", headers=auth(student))
    assert response.status_code == 200
    assert response.json()["sittings_kept"] == 1

    db.expire_all()
    assert db.get(OsceCircuit, circuit_id) is None
    kept = db.get(OsceSession, sitting_id)
    assert kept is not None, "the attempt must survive"
    assert kept.circuit_id is None
    assert client.get("/api/osce/circuits", headers=auth(student)).json() == []


def test_another_candidates_circuit_cannot_be_deleted(client, db, student, admin):
    from app.constants import ROLE_STUDENT
    from app.models import OsceCircuit
    from tests.conftest import _make_user

    station = make_station(db)
    circuit = OsceCircuit(user_id=student.id, title="Mine", station_ids=[station.id])
    db.add(circuit)
    db.commit()

    intruder = _make_user(db, "nosy@example.com", ROLE_STUDENT)
    assert client.delete(
        f"/api/osce/circuits/{circuit.id}", headers=auth(intruder)
    ).status_code == 403
    # An administrator may tidy anyone's.
    assert client.delete(
        f"/api/osce/circuits/{circuit.id}", headers=auth(admin)
    ).status_code == 200


def test_deleting_a_circuit_that_is_not_there_is_a_404(client, db, student):
    assert client.delete("/api/osce/circuits/9999", headers=auth(student)).status_code == 404


def test_a_question_s_scan_does_not_count_as_the_station_s_opening_image(db):
    """Station 158 asked the candidate to examine eye movements over a brain MRI.

    Its question C owns that MRI, correctly. But every count of "does this
    station have an image" included it, so the station looked covered, no gaze
    montage was ever searched for, and the opening task had nothing to examine.
    """
    from app.models import Image, OsceFigure
    from app.services.osce.station_images import (
        opening_figures,
        opening_image_is_settled,
        stations_needing_images,
    )

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the patient's eye movements.",
         "seconds": 270, "rubric": [{"text": "Identifies the gaze palsy", "marks": 10}]},
        {"label": "C", "text": "What does this scan show?", "seconds": 90,
         "image_wanted": "MRI of the brain showing white matter lesions",
         "rubric": [{"text": "Reads the scan", "marks": 5}]},
    ])
    image = Image(sha256="9" * 64, content_type="image/jpeg", data=b"jpeg",
                  size_bytes=4, origin="pdf")
    db.add(image)
    db.flush()
    mri = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                     is_approved=True, verification_status="faithful",
                     match_confidence=0.95)
    db.add(mri)
    db.flush()
    station.prompts = [
        station.prompts[0], {**station.prompts[1], "figure_id": mri.id},
    ]
    db.commit()
    db.refresh(station)

    assert opening_figures(station) == [], "the MRI belongs to question C"
    assert not opening_image_is_settled(station), "so nothing opens the station"
    assert station.id in stations_needing_images(db), "and it still needs one"


def test_generating_one_station_per_subspecialty_asks_for_all_nine(client, db, admin):
    """The button is "a fresh circuit's worth", not "top up my thinnest area".

    Topping up to a target answers a different question and generates nothing
    at all once every subspecialty is full.
    """
    from app.constants import SUBSPECIALTIES
    from app.models import Job

    make_station(db, subspecialty="Cataract")

    response = client.post(
        "/api/osce/stations/generate", json={"one_each": True}, headers=auth(admin)
    )
    assert response.status_code == 202
    body = response.json()
    assert body["total"] == 9
    assert body["plan"] == {name: 1 for name in SUBSPECIALTIES}

    job = db.get(Job, body["job_id"])
    assert job.payload["per_subspecialty"] == {name: 1 for name in SUBSPECIALTIES}


def test_generated_stations_have_their_images_sourced(db, admin):
    """A generated station arrives complete except for having nothing to show.

    Its findings are already split and its questions already in the examiner
    arc, so image sourcing is the only link missing - and without it the
    station asks the candidate to examine something it cannot show them.
    """
    from app.models import Job
    from app.models.ops import JOB_PENDING
    from app.services.generate.stations import _queue_image_sourcing
    from app.services.jobs.runner import JobContext
    from app.services.osce.station_images import JOB_SOURCE_STATION_IMAGES

    generation = Job(job_type="generate_osce_stations", status=JOB_PENDING,
                     payload={"per_subspecialty": {"Cataract": 1}}, cursor={},
                     result={"created": [31, 30], "failed": ["Glaucoma"]},
                     created_by_id=admin.id)
    db.add(generation)
    db.commit()

    _queue_image_sourcing(JobContext(db=db, job=generation))
    db.commit()

    sourcing = db.query(Job).filter_by(job_type=JOB_SOURCE_STATION_IMAGES).one()
    assert sourcing.payload["station_ids"] == [30, 31]
    assert sourcing.payload["only_missing"] is True


def test_an_image_can_be_supplied_by_hand_when_no_search_can_find_one(
    client, db, admin
):
    """A Hess chart, a forced duction test, an A-scan printout.

    Some investigations are not on the open web. The pipeline could detach a
    figure's image but never attach one, so those questions could not be fixed
    by anybody - a search that comes back empty had nowhere to hand over to.
    """
    import io

    from app.models import Image, OsceFigure

    station = make_station(db)
    figure = OsceFigure(station_id=station.id, position=0,
                        verification_status="rejected", is_approved=False,
                        described_findings="The examiner states the findings.",
                        described_findings_approved=True)
    db.add(figure)
    db.commit()

    response = client.post(
        f"/api/osce/figures/{figure.id}/image",
        headers=auth(admin),
        files={"image": ("hess.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 900), "image/png")},
        data={"caption": "Orthoptic Hess chart, right eye"},
    )
    assert response.status_code == 201

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(id=figure.id).one()
    assert figure.image_id is not None
    assert figure.is_approved is True, "a supplied image is trusted and shown"
    assert figure.verification_status == "supplied"
    assert figure.caption == "Orthoptic Hess chart, right eye"
    # The stated findings were a substitute for the missing image; with the
    # image there they would be read out over the top of it.
    assert figure.described_findings is None
    assert db.get(Image, figure.image_id).origin == "upload"


def test_a_file_that_is_not_an_image_is_refused(client, db, admin):
    import io

    from app.models import OsceFigure

    station = make_station(db)
    figure = OsceFigure(station_id=station.id, position=0)
    db.add(figure)
    db.commit()

    response = client.post(
        f"/api/osce/figures/{figure.id}/image",
        headers=auth(admin),
        files={"image": ("notes.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
    )
    assert response.status_code == 400
    assert "not an image" in response.json()["detail"]
