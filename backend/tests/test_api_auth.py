"""Authentication and the admin/candidate boundary.

This is the only thing standing between a candidate and the marking keys for
questions they are about to sit, so every admin-only route is checked rather
than a representative sample.
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_PASSWORD as PASSWORD
from tests.conftest import auth


def test_login_returns_a_working_token(client, student):
    response = client.post(
        "/api/auth/login", json={"email": student.email, "password": PASSWORD}
    )
    assert response.status_code == 200
    token = response.json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == student.email
    assert me.json()["role"] == "student"


def test_wrong_password_and_unknown_email_are_indistinguishable(client, student):
    """The message must not let an attacker enumerate who has an account."""
    wrong = client.post(
        "/api/auth/login", json={"email": student.email, "password": "not-the-password"}
    )
    missing = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_disabled_account_cannot_sign_in(client, db, student):
    student.is_active = False
    db.commit()
    response = client.post(
        "/api/auth/login", json={"email": student.email, "password": PASSWORD}
    )
    assert response.status_code == 403


def test_a_token_for_a_disabled_account_stops_working(client, db, student):
    headers = auth(student)
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    student.is_active = False
    db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_no_token_and_a_junk_token_are_both_rejected(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"}
    ).status_code == 401


def test_password_change_requires_the_current_password(client, student):
    headers = auth(student)
    bad = client.post(
        "/api/auth/change-password",
        json={"current_password": "guessing", "new_password": "a-brand-new-passphrase"},
        headers=headers,
    )
    assert bad.status_code == 400

    good = client.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
        headers=headers,
    )
    assert good.status_code == 204
    assert client.post(
        "/api/auth/login",
        json={"email": student.email, "password": "a-brand-new-passphrase"},
    ).status_code == 200


def test_short_passwords_are_refused(client, student):
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=auth(student),
    )
    assert response.status_code == 422


# Every route a candidate must not reach. Reviewing a station exposes its
# rubric and diagnosis; the settings routes expose API keys.
ADMIN_ONLY = [
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/settings"),
    ("GET", "/api/admin/errors"),
    ("GET", "/api/admin/settings/ai-routing"),
    ("GET", "/api/admin/spend"),
    ("GET", "/api/admin/stats"),
    ("GET", "/api/admin/invites"),
    ("GET", "/api/admin/jobs"),
    ("GET", "/api/osce/figures"),
    ("GET", "/api/papers/availability"),
    ("GET", "/api/figures/image-quota"),
    ("POST", "/api/osce/stations/build-prompts"),
    ("POST", "/api/osce/stations/split-findings"),
    ("POST", "/api/osce/stations/source-images"),
    ("POST", "/api/questions/generate-model-answers"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY)
def test_candidates_are_refused_admin_routes(client, student, method, path):
    response = client.request(method, path, headers=auth(student), json={})
    assert response.status_code == 403, f"{method} {path} let a candidate through"


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY)
def test_admin_routes_need_a_token_at_all(client, method, path):
    response = client.request(method, path, json={})
    assert response.status_code == 401


def test_station_preview_is_admin_only(client, student, admin, db):
    """The preview is every question, its rubric and the diagnosis."""
    from app.models import OsceStation

    station = OsceStation(
        title="Corneal scar", subspecialty="Cornea & External Eye", total_marks=20,
        source="generated", status="approved", diagnosis="Herpetic stromal keratitis",
        prompts_status="complete",
        prompts=[{"label": "A", "text": "Examine.", "seconds": 540, "rubric": []}],
    )
    db.add(station)
    db.commit()

    assert client.get(
        f"/api/osce/stations/{station.id}/preview", headers=auth(student)
    ).status_code == 403
    allowed = client.get(
        f"/api/osce/stations/{station.id}/preview", headers=auth(admin)
    )
    assert allowed.status_code == 200
    assert allowed.json()["diagnosis"] == "Herpetic stromal keratitis"
