"""Minimal SMTP email sender for delivering generated documents.

Configured via env (see app.config):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
    SMTP_USE_TLS (default true), SMTP_USE_SSL (default false)
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable, Sequence

from app.config import settings

log = logging.getLogger(__name__)

# (filename, content_bytes, mime_type) e.g. ("doc.docx", b"...", "application/...")
Attachment = tuple[str, bytes, str]


class EmailConfigError(RuntimeError):
    """Raised when an email send is attempted without SMTP configured."""


def email_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send_email(
    *,
    to: Sequence[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    attachments: Iterable[Attachment] = (),
) -> None:
    if not email_configured():
        raise EmailConfigError(
            "SMTP is not configured. Set SMTP_HOST and SMTP_FROM (and credentials)."
        )
    recipients = [addr.strip() for addr in to if addr and addr.strip()]
    if not recipients:
        raise ValueError("At least one recipient email address is required")
    attachment_list = list(attachments)

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    for filename, content, mime_type in attachment_list:
        maintype, _, subtype = mime_type.partition("/")
        msg.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )

    host, port = settings.smtp_host, settings.smtp_port
    log.info("Sending email to %s via %s:%s (%d attachment(s))", recipients, host, port, len(attachment_list))

    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            _login_and_send(server, msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if settings.smtp_use_tls:
                server.starttls()
            _login_and_send(server, msg)


def _login_and_send(server: smtplib.SMTP, msg: EmailMessage) -> None:
    if settings.smtp_user and settings.smtp_password:
        server.login(settings.smtp_user, settings.smtp_password)
    server.send_message(msg)
