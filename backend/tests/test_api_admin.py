"""Admin portal: settings and secrets, users, invites, documents, jobs.

The settings tests matter most. API keys live in that table Fernet-encrypted,
and the masked-value rule is what stops the UI writing asterisks back over a
real key - which would make every stored key unusable with no way to tell.
"""

from __future__ import annotations

import io

from app.constants import ROLE_ADMIN, ROLE_STUDENT
from app.models import Invite, Setting, User
from tests.conftest import auth


# --- Settings and secrets ------------------------------------------------
def test_a_secret_is_stored_encrypted_and_only_ever_returned_masked(client, db, admin):
    response = client.put(
        "/api/admin/settings",
        json={"settings": [{"key": "ai.api_key", "value": "sk-a-real-secret-key"}]},
        headers=auth(admin),
    )
    assert response.status_code == 200

    stored = db.get(Setting, "ai.api_key")
    db.refresh(stored)
    assert stored.is_encrypted is True
    assert "sk-a-real-secret-key" not in str(stored.value)

    served = client.get("/api/admin/settings", headers=auth(admin)).json()["settings"]
    key_row = next(s for s in served if s["key"] == "ai.api_key")
    assert "sk-a-real-secret-key" not in str(key_row)


def test_writing_back_a_masked_secret_leaves_the_real_key_alone(client, db, admin):
    """The UI shows asterisks; saving the form must not overwrite the key."""
    from app.services.settings_store import SettingsStore

    client.put(
        "/api/admin/settings",
        json={"settings": [{"key": "ai.api_key", "value": "sk-the-original"}]},
        headers=auth(admin),
    )
    client.put(
        "/api/admin/settings",
        json={"settings": [{"key": "ai.api_key", "value": "************"}]},
        headers=auth(admin),
    )
    db.expire_all()
    assert SettingsStore(db).get_str("ai.api_key") == "sk-the-original"


def test_clearing_a_setting_restores_its_default(client, db, admin):
    from app.services.settings_store import SettingsStore

    client.put(
        "/api/admin/settings",
        json={"settings": [{"key": "osce.stations_per_circuit", "value": 4}]},
        headers=auth(admin),
    )
    db.expire_all()
    assert SettingsStore(db).get_int("osce.stations_per_circuit") == 4

    client.put(
        "/api/admin/settings",
        json={"settings": [{"key": "osce.stations_per_circuit", "value": ""}]},
        headers=auth(admin),
    )
    db.expire_all()
    assert SettingsStore(db).get_int("osce.stations_per_circuit") == 9


def test_the_ai_connection_test_reports_the_provider_and_reply(client, db, admin, ai):
    ai.responder = lambda body, n: "ready"
    response = client.post(
        "/api/admin/settings/test-ai",
        json={"task": "structuring", "prompt": "Reply with: ready"},
        headers=auth(admin),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reply"] == "ready"
    assert body["slot"] == "primary"

    # The test is logged as a connection test, not against the borrowed task.
    from app.models import AiCall

    assert [c.task for c in db.query(AiCall).all()] == ["connection_test"]


def test_the_connection_test_says_which_task_has_no_key(client, db, admin):
    response = client.post(
        "/api/admin/settings/test-ai", json={"task": "grading"}, headers=auth(admin)
    )
    assert response.status_code == 400
    assert "no API key set" in response.json()["detail"]


def test_a_provider_error_surfaces_as_a_bad_gateway_not_a_500(client, db, admin, ai):
    from app.services.ai.client import AIError

    def refuse(body, n):
        raise AIError("HTTP 401: invalid api key")

    ai.responder = refuse
    response = client.post(
        "/api/admin/settings/test-ai", json={"task": "structuring"}, headers=auth(admin)
    )
    assert response.status_code == 502
    assert "invalid api key" in response.json()["detail"]


def test_routing_shows_which_provider_serves_each_task(client, db, admin, ai):
    routing = client.get(
        "/api/admin/settings/ai-routing", headers=auth(admin)
    ).json()["routing"]
    tasks = {row["task"]: row for row in routing}
    assert set(tasks) >= {"structuring", "grading", "vision", "model_answer"}
    assert tasks["grading"]["configured"] is True
    assert tasks["grading"]["provider"] == "openrouter"


# --- Users and invites ---------------------------------------------------
def test_an_administrator_can_create_and_disable_a_candidate(client, db, admin):
    created = client.post(
        "/api/admin/users",
        json={
            "email": "New.Candidate@Example.com",
            "full_name": "New Candidate",
            "password": "a-long-enough-passphrase",
            "role": ROLE_STUDENT,
        },
        headers=auth(admin),
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    assert created.json()["email"] == "new.candidate@example.com", "emails normalise"

    disabled = client.patch(
        f"/api/admin/users/{user_id}", json={"is_active": False}, headers=auth(admin)
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert client.post(
        "/api/auth/login",
        json={"email": "new.candidate@example.com", "password": "a-long-enough-passphrase"},
    ).status_code == 403


def test_a_duplicate_email_is_refused(client, db, admin, student):
    response = client.post(
        "/api/admin/users",
        json={"email": student.email, "password": "another-long-passphrase"},
        headers=auth(admin),
    )
    assert response.status_code in {400, 409}


def test_an_invite_is_redeemed_once_and_only_once(client, db, admin):
    invite = client.post(
        "/api/admin/invites",
        json={"email": "invited@example.com", "role": ROLE_STUDENT, "expires_in_days": 7},
        headers=auth(admin),
    ).json()
    code = invite["code"]

    redeemed = client.post(
        "/api/auth/redeem-invite",
        json={
            "code": code.lower(),  # case-insensitive
            "email": "invited@example.com",
            "full_name": "Invited Person",
            "password": "a-long-enough-passphrase",
        },
    )
    assert redeemed.status_code == 201
    assert redeemed.json()["user"]["role"] == ROLE_STUDENT

    again = client.post(
        "/api/auth/redeem-invite",
        json={
            "code": code,
            "email": "someone.else@example.com",
            "password": "a-long-enough-passphrase",
        },
    )
    assert again.status_code == 400
    assert "already been used" in again.json()["detail"]


def test_an_invite_issued_for_one_address_cannot_be_used_by_another(client, db, admin):
    code = client.post(
        "/api/admin/invites",
        json={"email": "intended@example.com"},
        headers=auth(admin),
    ).json()["code"]

    response = client.post(
        "/api/auth/redeem-invite",
        json={
            "code": code,
            "email": "opportunist@example.com",
            "password": "a-long-enough-passphrase",
        },
    )
    assert response.status_code == 400
    assert "different email" in response.json()["detail"]


def test_an_expired_invite_is_refused(client, db, admin):
    from datetime import datetime, timedelta, timezone

    from app.security import generate_invite_code

    invite = Invite(
        code=generate_invite_code(),
        role=ROLE_STUDENT,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        created_by_id=admin.id,
    )
    db.add(invite)
    db.commit()

    response = client.post(
        "/api/auth/redeem-invite",
        json={
            "code": invite.code,
            "email": "late@example.com",
            "password": "a-long-enough-passphrase",
        },
    )
    assert response.status_code == 400
    assert "expired" in response.json()["detail"]


def test_a_used_invite_cannot_be_deleted(client, db, admin):
    code = client.post("/api/admin/invites", json={}, headers=auth(admin)).json()
    client.post(
        "/api/auth/redeem-invite",
        json={
            "code": code["code"],
            "email": "redeemer@example.com",
            "password": "a-long-enough-passphrase",
        },
    )
    response = client.delete(f"/api/admin/invites/{code['id']}", headers=auth(admin))
    assert response.status_code == 400


def test_an_unknown_role_is_refused_on_an_invite(client, admin):
    response = client.post(
        "/api/admin/invites", json={"role": "examiner"}, headers=auth(admin)
    )
    assert response.status_code == 400


# --- Documents -----------------------------------------------------------
def upload(client, admin, name: str, body: bytes, **form):
    return client.post(
        "/api/documents",
        headers=auth(admin),
        files={"file": (name, io.BytesIO(body), "text/plain")},
        data={"start_ingestion": "false", **form},
    )


def test_a_text_report_uploads_and_reports_what_was_detected(client, db, admin):
    report = (
        "Question 1 (10 marks)\n"
        "A 62-year-old presents with acute angle closure.\n"
        "a) List five immediate management steps. (5 marks)\n"
        "b) Describe the definitive treatment. (5 marks)\n"
    )
    response = upload(client, admin, "report.txt", report.encode(), exam_period="2026 Semester 1")
    assert response.status_code == 201
    body = response.json()
    assert body["document"]["exam_period"] == "2026 Semester 1"
    assert body["document"]["size_bytes"] == len(report.encode())
    assert body["job_id"] == 0, "ingestion was not requested"

    listed = client.get("/api/documents", headers=auth(admin)).json()
    assert len(listed) == 1
    assert listed[0]["question_count"] == 0


def test_the_same_file_cannot_be_uploaded_twice(client, db, admin):
    upload(client, admin, "report.txt", b"Question 1 (5 marks)\nSomething.\n")
    again = upload(client, admin, "renamed.txt", b"Question 1 (5 marks)\nSomething.\n")
    assert again.status_code == 409
    assert "already uploaded" in again.json()["detail"]


def test_an_unsupported_file_type_is_refused_before_it_is_stored(client, db, admin):
    response = upload(client, admin, "notes.pptx", b"anything")
    assert response.status_code == 400
    assert "Accepted" in response.json()["detail"]
    assert client.get("/api/documents", headers=auth(admin)).json() == []


def test_an_empty_upload_is_refused(client, admin):
    response = upload(client, admin, "empty.txt", b"")
    assert response.status_code == 400


# The segmenter splits deterministically on a header that stands on its own
# line, which is how the real reports set them out.
REPORT = b"""SEQ 1

A 62-year-old presents with acute angle closure.
a) List five immediate management steps. (5 marks)
b) Describe the definitive treatment. (5 marks)

SEQ 2

A 40-year-old has a slowly enlarging conjunctival lesion.
a) Give four differential diagnoses. (4 marks)
"""


def test_a_document_preview_costs_no_model_call(client, db, admin, ai):
    """Checking a new format must not spend tokens to find out it parsed."""
    document_id = upload(client, admin, "report.txt", REPORT).json()["document"]["id"]

    preview = client.get(f"/api/documents/{document_id}/preview", headers=auth(admin))
    assert preview.status_code == 200
    assert [b["label"] for b in preview.json()["blocks"]] == ["SEQ 1", "SEQ 2"]
    assert ai.requests == [], "segmentation is deterministic and must cost nothing"


def test_the_detected_kind_and_block_count_come_back_with_the_upload(client, db, admin):
    response = upload(client, admin, "written.txt", REPORT)
    assert response.json()["detected_kind"] == "written"
    assert response.json()["detected_blocks"] == 2


def test_a_document_with_questions_is_not_deleted_by_accident(client, db, admin):
    from app.models import Question, SourceDocument

    document_id = upload(client, admin, "report.txt", b"Question 1 (5 marks)\nStem.\n").json()[
        "document"
    ]["id"]
    db.add(
        Question(
            question_type="SEQ", stem="from that document", total_marks=5,
            status="approved", source="past_paper", source_document_id=document_id,
        )
    )
    db.commit()

    refused = client.delete(f"/api/documents/{document_id}", headers=auth(admin))
    assert refused.status_code == 409
    assert "delete_questions=true" in refused.json()["detail"]

    forced = client.delete(
        f"/api/documents/{document_id}?delete_questions=true", headers=auth(admin)
    )
    assert forced.status_code == 204
    assert db.query(SourceDocument).count() == 0
    assert db.query(Question).count() == 0


# --- Jobs and telemetry --------------------------------------------------
def test_a_job_is_visible_to_its_owner_and_to_an_admin_but_not_to_others(
    client, db, admin, student
):
    from app.services.jobs.runner import create_job

    job = create_job(db, "generate_model_answers", payload={"question_ids": []},
                     created_by_id=admin.id, total_steps=1)

    assert client.get(f"/api/jobs/{job.id}", headers=auth(admin)).status_code == 200
    assert client.get(f"/api/jobs/{job.id}", headers=auth(student)).status_code == 403
    assert client.get("/api/jobs/9999", headers=auth(admin)).status_code == 404


def test_a_queued_job_can_be_cancelled(client, db, admin):
    from app.services.jobs.runner import create_job

    job = create_job(db, "generate_model_answers", payload={}, created_by_id=admin.id)
    response = client.post(f"/api/admin/jobs/{job.id}/cancel", headers=auth(admin))
    assert response.status_code == 200
    db.expire_all()
    assert client.get(f"/api/jobs/{job.id}", headers=auth(admin)).json()["status"] == "cancelled"


def test_a_failing_job_records_the_reason_in_the_error_log(client, db, admin, run_jobs):
    from app.services.jobs.runner import create_job

    create_job(db, "generate_model_answers", payload={}, created_by_id=admin.id)
    run_jobs()

    job = client.get("/api/admin/jobs", headers=auth(admin)).json()[0]
    assert job["status"] == "failed"
    assert "question_ids" in job["error"]

    errors = client.get("/api/admin/errors", headers=auth(admin)).json()
    assert any("question_ids" in e["message"] for e in errors)


def test_an_unknown_job_type_fails_cleanly_rather_than_hanging(client, db, admin, run_jobs):
    from app.services.jobs.runner import create_job

    create_job(db, "a_job_type_that_does_not_exist", payload={}, created_by_id=admin.id)
    run_jobs()
    job = client.get("/api/admin/jobs", headers=auth(admin)).json()[0]
    assert job["status"] == "failed"
    assert "No handler registered" in job["error"]


def test_the_dashboard_counts_content_and_spend(client, db, admin, ai):
    from tests.test_api_exams import make_question

    make_question(db)
    make_question(db, status="draft")
    ai.responder = lambda body, n: "ready"
    client.post(
        "/api/admin/settings/test-ai", json={"task": "structuring"}, headers=auth(admin)
    )

    stats = client.get("/api/admin/stats", headers=auth(admin)).json()
    assert stats["questions_total"] == 2
    assert stats["questions_by_status"]["approved"] == 1
    assert stats["ai_last_30_days"]["calls"] == 1
    assert stats["ai_last_30_days"]["prompt_tokens"] == 100

    spend = client.get("/api/admin/spend", headers=auth(admin)).json()
    assert spend["rows"][0]["task"] == "connection_test"
    assert spend["rows"][0]["calls"] == 1


def test_the_error_log_can_be_pruned(client, db, admin):
    from app.services.errors import log_error

    for i in range(5):
        log_error(db, source="test", message=f"problem {i}")

    assert len(client.get("/api/admin/errors", headers=auth(admin)).json()) == 5
    response = client.delete("/api/admin/errors?keep=2", headers=auth(admin))
    assert response.json()["deleted"] == 3
    assert len(client.get("/api/admin/errors", headers=auth(admin)).json()) == 2


# --- Health --------------------------------------------------------------
def test_health_and_readiness_need_no_token(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] == "connected"


def test_the_bootstrap_admin_is_created_and_promoted(db):
    """Startup must be able to produce an administrator on a fresh database."""
    from app.config import settings
    from app.startup import bootstrap_admin_user

    original = (settings.bootstrap_admin_email, settings.bootstrap_admin_password)
    settings.bootstrap_admin_email = "boss@example.com"
    settings.bootstrap_admin_password = "a-long-enough-passphrase"
    try:
        bootstrap_admin_user(db)
        db.commit()
        created = db.query(User).filter_by(email="boss@example.com").one()
        assert created.role == ROLE_ADMIN

        # Running again on an existing student promotes rather than duplicating.
        created.role = ROLE_STUDENT
        db.commit()
        bootstrap_admin_user(db)
        db.commit()
        db.refresh(created)
        assert created.role == ROLE_ADMIN
        assert db.query(User).filter_by(email="boss@example.com").count() == 1
    finally:
        settings.bootstrap_admin_email, settings.bootstrap_admin_password = original


def test_the_subspecialty_breakdown_accounts_for_every_question(client, db, admin):
    """It sat beside questions_total and quietly disagreed with it.

    Questions with no subspecialty were filtered out of the breakdown, so the
    dashboard showed "99 questions" above a list adding up to 98.
    """
    from tests.test_api_exams import make_question

    make_question(db, subspecialty="Glaucoma")
    make_question(db, subspecialty="Cataract")
    make_question(db, subspecialty=None)

    stats = client.get("/api/admin/stats", headers=auth(admin)).json()
    breakdown = stats["questions_by_subspecialty"]
    assert sum(breakdown.values()) == stats["questions_total"] == 3
    assert breakdown["Unclassified"] == 1


def test_an_upload_and_its_ingestion_job_land_together(client, db, admin):
    """They were two commits, and the gap between them lost a real report.

    A free-tier instance restarting mid-upload stored the document and never
    queued the job: nothing running, nothing failed, nothing in the error log,
    and a 160-page report sitting at "uploaded" with zero items.
    """
    from app.models import Job, SourceDocument

    response = client.post(
        "/api/documents",
        headers=auth(admin),
        files={"file": ("report.txt", io.BytesIO(b"Question 1 (5 marks)\nDiscuss.\n"), "text/plain")},
        data={"start_ingestion": "true"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["job_id"] != 0, "ingestion was requested, so it must be queued"

    db.expire_all()
    document = db.get(SourceDocument, body["document"]["id"])
    job = db.get(Job, body["job_id"])
    assert document is not None
    assert job is not None, "a stored document with no job is the failure this guards"
    assert job.payload["document_id"] == document.id


def test_a_document_stored_without_ingestion_is_still_committed(client, db, admin):
    """The other half: no job to carry the commit, so the endpoint must."""
    from app.models import SourceDocument

    response = upload(client, admin, "report.txt", b"Question 1 (5 marks)\nDiscuss.\n")
    assert response.status_code == 201
    assert response.json()["job_id"] == 0

    db.expire_all()
    assert db.get(SourceDocument, response.json()["document"]["id"]) is not None
