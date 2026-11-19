"""Email providers (BE-18). The service talks to the EmailProvider interface only.

ConsoleEmailProvider: logs the email, sends nothing (default; local and tests).
SmtpEmailProvider: sends through SMTP, with a timeout from SMTP_TIMEOUT.

A provider raises on failure; the caller (notifications.service) catches and logs it.
"""

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger("app.notifications")


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/pdf"


@dataclass(frozen=True)
class EmailMessage:
    """A rendered email: plain strings and bytes only, no ORM objects (it is sent after the
    commit, when the session's objects are expired)."""

    to: str
    subject: str
    text: str
    html: str | None = None
    attachments: tuple[Attachment, ...] = field(default=())
    # The body holds a secret (a verification link): never write it to a log.
    contains_secret: bool = False


class EmailProvider(Protocol):
    def send(self, message: EmailMessage) -> None:
        """Deliver the email or raise."""


class ConsoleEmailProvider:
    """Logs the email instead of sending it. Attachments are listed by name and size only.

    The body and the full address are logged only with log_body (local development). Anywhere
    else (a server left on console) only the recipient's domain and the subject are: a body
    holds names and links. A body with a secret link (contains_secret) is never logged.
    """

    def __init__(self, log_body: bool = True) -> None:
        self._log_body = log_body

    def send(self, message: EmailMessage) -> None:
        files = ", ".join(f"{a.filename} ({len(a.content)} B)" for a in message.attachments)
        if not self._log_body:
            logger.info(
                "email (console, not sent) to domain %s subject=%r attachments=[%s]",
                recipient_domain(message.to),
                message.subject,
                files,
            )
            return
        body = (
            "[body not logged: it contains a secret link]"
            if message.contains_secret
            else message.text
        )
        logger.info(
            "email (console, not sent) to=%s subject=%r attachments=[%s]\n%s",
            message.to,
            message.subject,
            files,
            body,
        )


def recipient_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else "?"


def build_mime(message: EmailMessage, sender: str) -> MimeMessage:
    """Plain text, an HTML alternative if any, then the attachments."""
    mime = MimeMessage()
    mime["From"] = sender
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip("> "))
    mime.set_content(message.text)
    if message.html is not None:
        mime.add_alternative(message.html, subtype="html")
    for attachment in message.attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        mime.add_attachment(
            attachment.content, maintype=maintype, subtype=subtype, filename=attachment.filename
        )
    return mime


class SmtpEmailProvider:
    """One SMTP connection per email (few emails per action; no pooling needed in the MVP).

    The timeout covers connecting and every SMTP command, so a slow or silent server makes
    send() raise (socket timeout) after at most SMTP_TIMEOUT per step, instead of hanging
    the request indefinitely. The request still waits for each send (see service.py).
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host:
            raise ValueError("SmtpEmailProvider needs SMTP_HOST")
        self._settings = settings

    def send(self, message: EmailMessage) -> None:
        s = self._settings
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(build_mime(message, s.email_from))
