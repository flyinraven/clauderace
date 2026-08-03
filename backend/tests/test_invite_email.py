"""Invites reaching the candidate by email.

The whole point of this path is that it follows the settings table: an admin
who changes the mailbox, the port or the from address gets the new one on the
very next send. The tests therefore drive it by writing settings rather than by
passing configuration in, and intercept at `smtplib` so the transport choice
(implicit SSL vs STARTTLS) is covered too.
"""

from __future__ import annotations

import smtplib

import pytest

from app.constants import ROLE_STUDENT
from app.models import ErrorLog, Invite, Setting
from tests.conftest import auth


class FakeSMTP:
    """Stands in for a server. Records what it was asked to do."""

    sent: list[dict] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in_as: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return None

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in_as = username

    def send_message(self, message):
        FakeSMTP.sent.append(
            {
                "host": self.host,
                "port": self.port,
                "tls": self.started_tls,
                "username": self.logged_in_as,
                "from": message["From"],
                "to": message["To"],
                "subject": message["Subject"],
                "body": "\n".join(
                    part.get_content() for part in message.walk() if not part.is_multipart()
                ),
            }
        )


class RefusingSMTP(FakeSMTP):
    def send_message(self, message):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication credentials invalid")


@pytest.fixture()
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def configure_email(db, **overrides):
    values = {
        "smtp.enabled": True,
        "smtp.host": "mail.example.com",
        "smtp.port": 465,
        "smtp.use_ssl": True,
        "smtp.username": "exams@example.com",
        "smtp.from_address": "exams@example.com",
        "app.public_url": "https://exam.example.com",
    }
    values.update(overrides)
    for key, value in values.items():
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value, is_encrypted=False))
        else:
            row.value = value
    db.commit()


def create_invite(client, admin, email="trainee@example.com"):
    return client.post(
        "/api/admin/invites",
        json={"email": email, "role": ROLE_STUDENT, "expires_in_days": 7},
        headers=auth(admin),
    )


def test_an_invite_with_an_address_is_emailed_when_sending_is_on(client, db, admin, smtp):
    configure_email(db)

    response = create_invite(client, admin)

    assert response.status_code == 201
    body = response.json()
    assert body["email_sent"] is True
    assert body["email_error"] is None

    (message,) = smtp.sent
    assert message["to"] == "trainee@example.com"
    assert message["host"] == "mail.example.com"
    assert body["code"] in message["body"], "the candidate needs the code itself"
    # HashRouter: the route lives after the '#', or the link loses the code.
    assert f"https://exam.example.com/#/login?invite={body['code']}" in message["body"]


def test_sending_follows_the_settings_as_they_are_changed(client, db, admin, smtp):
    """Change the mailbox and the next invite goes out through the new one -
    including dropping from implicit SSL to a STARTTLS upgrade on port 587."""
    configure_email(db)
    create_invite(client, admin)
    assert smtp.sent[-1]["host"] == "mail.example.com"
    assert smtp.sent[-1]["tls"] is False, "port 465 is already encrypted"

    client.put(
        "/api/admin/settings",
        json={
            "settings": [
                {"key": "smtp.host", "value": "mail.siteground.example"},
                {"key": "smtp.port", "value": 587},
                {"key": "smtp.use_ssl", "value": False},
                {"key": "smtp.username", "value": "noreply@txglobal.com.au"},
                {"key": "smtp.from_address", "value": "exams@txglobal.com.au"},
                {"key": "smtp.from_name", "value": "RACE Exams"},
            ]
        },
        headers=auth(admin),
    )

    create_invite(client, admin, email="second@example.com")

    latest = smtp.sent[-1]
    assert latest["host"] == "mail.siteground.example"
    assert latest["port"] == 587
    assert latest["tls"] is True
    assert latest["username"] == "noreply@txglobal.com.au"
    assert latest["from"] == "RACE Exams <exams@txglobal.com.au>"


def test_an_invite_still_exists_when_the_mail_server_refuses_it(
    client, db, admin, monkeypatch, smtp
):
    """A rejected password must not cost the admin the invite - the code is
    valid, the failure is reported, and it is written to the error log."""
    configure_email(db)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RefusingSMTP)

    response = create_invite(client, admin)

    assert response.status_code == 201
    body = response.json()
    assert body["email_sent"] is False
    assert "rejected the username or password" in body["email_error"]
    assert db.get(Invite, body["id"]) is not None

    logged = db.query(ErrorLog).filter(ErrorLog.source == "email.invite").all()
    assert len(logged) == 1


def test_nothing_is_sent_while_email_is_switched_off(client, db, admin, smtp):
    configure_email(db, **{"smtp.enabled": False})

    body = create_invite(client, admin).json()

    assert smtp.sent == []
    assert body["email_sent"] is False
    assert "off" in body["email_error"]


def test_an_invite_can_be_resent_after_the_settings_are_fixed(client, db, admin, smtp):
    configure_email(db, **{"smtp.enabled": False})
    invite_id = create_invite(client, admin).json()["id"]

    blocked = client.post(f"/api/admin/invites/{invite_id}/send", headers=auth(admin))
    assert blocked.status_code == 400

    configure_email(db)
    resent = client.post(f"/api/admin/invites/{invite_id}/send", headers=auth(admin))

    assert resent.status_code == 200
    assert resent.json()["email_sent"] is True
    assert smtp.sent[-1]["to"] == "trainee@example.com"


def test_an_invite_with_no_address_is_left_to_be_copied_by_hand(client, db, admin, smtp):
    configure_email(db)

    body = client.post(
        "/api/admin/invites",
        json={"email": None, "role": ROLE_STUDENT},
        headers=auth(admin),
    ).json()

    assert smtp.sent == []
    assert body["email_sent"] is False
    assert body["email_error"] is None, "no address is a choice, not a failure"

    refused = client.post(f"/api/admin/invites/{body['id']}/send", headers=auth(admin))
    assert refused.status_code == 400


def test_the_test_email_goes_to_the_administrator_by_default(client, db, admin, smtp):
    configure_email(db)

    response = client.post("/api/admin/settings/test-email", json={}, headers=auth(admin))

    assert response.status_code == 200
    assert response.json()["to"] == admin.email
    assert smtp.sent[-1]["to"] == admin.email


def test_a_broken_mailbox_is_reported_by_the_test_rather_than_looking_fine(
    client, db, admin, monkeypatch, smtp
):
    configure_email(db)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RefusingSMTP)

    response = client.post("/api/admin/settings/test-email", json={}, headers=auth(admin))

    assert response.status_code == 502
    assert "rejected the username or password" in response.json()["detail"]
