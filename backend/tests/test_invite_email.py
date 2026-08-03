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
from app.services import email as email_service
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


class FakeResend:
    """Stands in for the Resend HTTPS API."""

    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body if body is not None else {"id": "re_123"}
        self.calls: list[dict] = []

    def client(self, timeout=None):
        outer = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None):
                outer.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
                return Response(outer.status_code, outer.body)

        return Client()


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture()
def resend(monkeypatch):
    fake = FakeResend()
    monkeypatch.setattr(email_service.httpx, "Client", lambda timeout=None: fake.client(timeout))
    return fake


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


# --- Resend (HTTPS) ------------------------------------------------------
def test_the_resend_provider_posts_over_https_instead_of_touching_smtp(
    client, db, admin, resend, smtp
):
    """The whole point of this route: Render's free tier blocks the SMTP ports,
    so nothing may reach out on 465 when Resend is the chosen provider."""
    configure_email(
        db,
        **{
            "email.provider": "resend",
            "resend.api_key": "re_test_key",
            "smtp.from_address": "exams@txglobal.com.au",
            "smtp.from_name": "RACE Exams",
        },
    )

    body = create_invite(client, admin).json()

    assert body["email_sent"] is True
    assert smtp.sent == [], "no SMTP connection may be attempted"

    (call,) = resend.calls
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    assert call["json"]["to"] == ["trainee@example.com"]
    assert call["json"]["from"] == "RACE Exams <exams@txglobal.com.au>"
    assert body["code"] in call["json"]["text"]
    assert "html" in call["json"]


def test_switching_provider_switches_route_with_no_other_change(client, db, admin, resend, smtp):
    configure_email(db, **{"email.provider": "smtp"})
    create_invite(client, admin)
    assert len(smtp.sent) == 1 and resend.calls == []

    client.put(
        "/api/admin/settings",
        json={
            "settings": [
                {"key": "email.provider", "value": "resend"},
                {"key": "resend.api_key", "value": "re_test_key"},
            ]
        },
        headers=auth(admin),
    )
    create_invite(client, admin, email="second@example.com")

    assert len(smtp.sent) == 1, "the SMTP route is not used again"
    assert len(resend.calls) == 1


def test_a_refusal_from_resend_is_reported_in_its_own_words(client, db, admin, resend):
    """An unverified sending domain is the likely first failure, and Resend
    says so plainly - that is more use than the status code."""
    configure_email(db, **{"email.provider": "resend", "resend.api_key": "re_test_key"})
    resend.status_code = 403
    resend.body = {"message": "The txglobal.com.au domain is not verified."}

    body = create_invite(client, admin).json()

    assert body["email_sent"] is False
    assert "not verified" in body["email_error"]
    assert db.get(Invite, body["id"]) is not None


def test_resend_without_a_key_says_which_field_is_missing(client, db, admin, resend):
    configure_email(db, **{"email.provider": "resend", "resend.api_key": ""})

    body = create_invite(client, admin).json()

    assert body["email_sent"] is False
    assert "Resend API key" in body["email_error"]
    assert resend.calls == [], "nothing is posted without a key"


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
