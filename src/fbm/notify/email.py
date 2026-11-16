"""Email notifications via Gmail SMTP (App Password). Every message is also logged to the outbox table."""

from __future__ import annotations

import smtplib
import sqlite3
import ssl
from email.message import EmailMessage

import markdown as md

from fbm import credentials
from fbm.notify.base import URGENT_KINDS, NotifyError, held_for_quiet_hours, in_quiet_hours, log_outbox
from fbm.settings import Settings, load_settings
from fbm.store import db

__all__ = ["URGENT_KINDS", "NotifyError", "deliver", "in_quiet_hours", "render_html", "send_email"]

SUBJECT_PREFIX = "[FBM] "
_STYLE = (
    "body{font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1f2328;max-width:760px}"
    "table{border-collapse:collapse;margin:8px 0}th,td{border:1px solid #d0d7de;padding:4px 8px;text-align:left}"
    "th{background:#f6f8fa}code{background:#f6f8fa;padding:1px 4px;border-radius:4px}"
    "h1,h2,h3{margin:12px 0 6px}.foot{color:#656d76;font-size:12px;margin-top:16px}"
)


def render_html(markdown_body: str) -> str:
    body = md.markdown(markdown_body, extensions=["tables", "sane_lists"])
    return (
        f"<html><head><style>{_STYLE}</style></head><body>{body}"
        "<p class='foot'>Sent by FBM — your fantasy front office. Fantasy data provided by Yahoo Fantasy.</p>"
        "</body></html>"
    )


def _log(conn: sqlite3.Connection, subject: str, body: str, status: str, detail: str = "") -> int:
    return log_outbox(conn, "email", subject, body, status, detail)


def deliver(subject: str, markdown_body: str, settings: Settings, smtp_factory=smtplib.SMTP_SSL) -> str:
    """Send one email now (no quiet-hours check, no outbox row). Returns the recipients; raises NotifyError."""
    try:
        sender = credentials.get(credentials.GMAIL_ADDRESS)
        password = credentials.get(credentials.GMAIL_APP_PASSWORD)
    except Exception as err:  # noqa: BLE001 - e.g. no OS keyring on a headless machine
        raise NotifyError(f"Gmail credentials unreadable ({type(err).__name__}). Run `fbm notify setup`.") from None
    if not sender or not password:
        raise NotifyError("Gmail not configured. Run `fbm notify setup`.")
    recipients = list(settings.recipients) or [sender]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = SUBJECT_PREFIX + subject, sender, ", ".join(recipients)
    msg.set_content(markdown_body)
    msg.add_alternative(render_html(markdown_body), subtype="html")
    try:
        with smtp_factory("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        raise NotifyError(f"Email send failed: {err}") from err
    return ", ".join(recipients)


def send_email(
    subject: str,
    markdown_body: str,
    kind: str = "report",
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    smtp_factory=smtplib.SMTP_SSL,
) -> dict[str, str]:
    """Email only (the `send_email` tool goes through fbm.notify.dispatch, which covers every channel).
    kind: report | approval | alert | retro. Non-urgent kinds are held during quiet hours (logged as 'held')."""
    settings = settings or load_settings()
    conn = conn or db.connect()
    logged = SUBJECT_PREFIX + subject
    if not settings.email_enabled:
        _log(conn, logged, markdown_body, "disabled")
        return {"status": "disabled"}
    if held_for_quiet_hours(settings, kind):
        _log(conn, logged, markdown_body, "held", "quiet hours")
        return {"status": "held", "detail": "quiet hours"}
    try:
        to = deliver(subject, markdown_body, settings, smtp_factory)
    except NotifyError as err:
        _log(conn, logged, markdown_body, "not_configured" if "not configured" in str(err) else "failed", str(err))
        raise
    _log(conn, logged, markdown_body, "sent", to)
    return {"status": "sent", "to": to}
