"""Outbound email, driven entirely by the admin-editable settings.

Two routes, chosen by `email.provider`:

* `resend` posts to an HTTPS API. This is the one that works in production -
  hosts block outbound SMTP to stop spam being sent from them. Render's free
  tier blocks ports 25, 465 and 587, and Railway blocks 465 too, so
  a mailbox connection there can only ever time out.
* `smtp` talks to a mailbox directly. Fine locally, or on a paid Render
  instance, and kept so the choice is a dropdown rather than a rewrite.

Nothing here is cached: the settings are read from the database on every send,
so changing provider or mailbox in the admin portal takes effect on the next
message without a redeploy. Sending is never allowed to be the reason an admin
action fails - callers get an `EmailError` to report, and the invite itself
still exists whether or not the mail went out.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from sqlalchemy.orm import Session

from app.models import Invite
from app.services.settings_store import SettingsStore


class EmailError(Exception):
    """Raised when a message could not be handed to the SMTP server."""


RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    provider: str
    api_key: str
    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    from_address: str
    from_name: str
    timeout_seconds: int

    @property
    def sender(self) -> str:
        """The envelope sender. Falls back to the username, which for a
        SiteGround-style mailbox is itself the address."""
        return (self.from_address or self.username).strip()

    def missing(self) -> list[str]:
        gaps = []
        if self.provider == "resend":
            if not self.api_key:
                gaps.append("Resend API key")
        else:
            if not self.host:
                gaps.append("SMTP host")
            if not self.port:
                gaps.append("SMTP port")
        if not self.sender:
            gaps.append("From address")
        return gaps


def load_email_config(db: Session) -> EmailConfig:
    store = SettingsStore(db)
    return EmailConfig(
        enabled=store.get_bool("smtp.enabled"),
        provider=store.get_str("email.provider", "smtp").strip().lower(),
        api_key=store.get_str("resend.api_key").strip(),
        host=store.get_str("smtp.host").strip(),
        port=store.get_int("smtp.port", 465),
        use_ssl=store.get_bool("smtp.use_ssl", True),
        username=store.get_str("smtp.username").strip(),
        password=store.get_str("smtp.password"),
        from_address=store.get_str("smtp.from_address").strip(),
        from_name=store.get_str("smtp.from_name").strip(),
        timeout_seconds=store.get_int("smtp.timeout_seconds", 20),
    )


def _connect(config: EmailConfig) -> smtplib.SMTP:
    """Open a connection using whichever transport the settings describe.

    Port 465 is implicit SSL; 587 and 25 start in the clear and upgrade. The
    'Use SSL' setting chooses between them, but a plain connection still
    upgrades opportunistically if the server offers STARTTLS, so a mistyped
    port degrades to a working-but-encrypted send rather than a plaintext one.
    """
    if config.use_ssl:
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
            context=ssl.create_default_context(),
        )
    server = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
    server.ehlo()
    if server.has_extn("starttls"):
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    return server


def send_email(
    db: Session,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    """Send one message. Raises `EmailError` with something an admin can act on."""
    config = load_email_config(db)
    if not config.enabled:
        raise EmailError("Email sending is switched off in Settings → Email notifications.")
    gaps = config.missing()
    if gaps:
        raise EmailError(f"Email is not configured: {', '.join(gaps)} not set.")

    if config.provider == "resend":
        _send_via_resend(config, to, subject, text_body, html_body)
    else:
        _send_via_smtp(config, to, subject, text_body, html_body)


def _send_via_resend(
    config: EmailConfig,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None,
) -> None:
    """Post the message to Resend's HTTPS API.

    Port 443, so no host's outbound SMTP block touches it.
    """
    payload: dict[str, object] = {
        "from": formataddr((config.from_name, config.sender))
        if config.from_name
        else config.sender,
        "to": [to],
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload["html"] = html_body

    try:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            response = client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {config.api_key}"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise EmailError(f"Resend did not respond within {config.timeout_seconds}s.") from exc
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach Resend: {exc}") from exc

    if response.status_code >= 400:
        # Resend explains refusals properly - an unverified sending domain or a
        # revoked key each say so - and that message is far more use to an
        # admin than the status code.
        detail = response.text[:400]
        try:
            detail = response.json().get("message") or detail
        except ValueError:
            pass
        raise EmailError(f"Resend refused the message (HTTP {response.status_code}): {detail}")


def _send_via_smtp(
    config: EmailConfig,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        formataddr((config.from_name, config.sender)) if config.from_name else config.sender
    )
    message["To"] = to
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        with _connect(config) as server:
            if config.username:
                server.login(config.username, config.password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            f"{config.host} rejected the username or password ({exc.smtp_code})."
        ) from exc
    except smtplib.SMTPException as exc:
        raise EmailError(f"{config.host} refused the message: {exc}") from exc
    except (OSError, ssl.SSLError) as exc:
        raise EmailError(
            f"Could not reach {config.host}:{config.port} - {exc}. Most hosts "
            f"block outbound SMTP to stop spam being sent from them: Render's "
            f"free tier blocks 25, 465 and 587, and Railway blocks 465 as well. "
            f"Try port 587 with SSL off before giving up - it is blocked less "
            f"often than 465 - and otherwise use the Resend provider, which "
            f"posts over HTTPS and is unaffected."
        ) from exc


# --- Invitations ----------------------------------------------------------
def invite_link(db: Session, code: str) -> str:
    """The URL that opens the sign-up form with the code already filled in.

    With no public URL configured there is nothing honest to link to, so the
    email carries the bare code instead.
    """
    base = SettingsStore(db).get_str("app.public_url").strip().rstrip("/")
    # The frontend is served by HashRouter - see frontend/src/main.tsx - so the
    # route lives after the '#'. A path-style link would land on the dashboard
    # and drop the code.
    return f"{base}/#/login?invite={code}" if base else ""


def send_invite_email(db: Session, invite: Invite) -> None:
    """Email one invite to the address it was issued for."""
    if not invite.email:
        raise EmailError("This invite has no email address, so there is nobody to send it to.")

    link = invite_link(db, invite.code)
    expires = (
        invite.expires_at.strftime("%d %B %Y") if invite.expires_at else "no expiry date"
    )
    role_line = (
        "You will be set up as an administrator."
        if invite.role == "admin"
        else "You will be set up as a candidate."
    )

    text = "\n".join(
        [
            "You have been invited to the RANZCO RACE Exam Simulator.",
            "",
            f"Invite code: {invite.code}",
            *( [f"Sign up here: {link}"] if link else
               ["Open the site, choose 'Use an invite code', and enter it there."] ),
            "",
            role_line,
            f"The code works once and expires on {expires}.",
        ]
    )

    action = (
        f'<p><a href="{link}" style="background:#0f766e;color:#fff;padding:10px 18px;'
        f'border-radius:6px;text-decoration:none;display:inline-block">Create my account</a></p>'
        if link
        else "<p>Open the site, choose &lsquo;Use an invite code&rsquo;, and enter it there.</p>"
    )
    html = (
        '<div style="font-family:system-ui,sans-serif;font-size:15px;color:#0f172a">'
        "<p>You have been invited to the <strong>RANZCO RACE Exam Simulator</strong>.</p>"
        f'<p>Invite code: <code style="background:#f1f5f9;padding:4px 8px;border-radius:4px;'
        f'font-size:16px;letter-spacing:1px">{invite.code}</code></p>'
        f"{action}"
        f"<p style=\"color:#475569;font-size:13px\">{role_line} "
        f"The code works once and expires on {expires}.</p>"
        "</div>"
    )

    send_email(
        db,
        to=invite.email,
        subject="Your invitation to the RACE Exam Simulator",
        text_body=text,
        html_body=html,
    )
