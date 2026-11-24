"""Stage 1 of BE-18: emails leave only after the commit, and a provider failure never
breaks the action."""

import logging
import socket
import threading
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import engine
from app.modules.identity.models import User
from app.modules.notifications import service as notifications
from app.modules.notifications.providers import (
    Attachment,
    ConsoleEmailProvider,
    EmailMessage,
    SmtpEmailProvider,
)
from tests.emails import RecordingEmailProvider
from tests.factories import create_user


def message(to: str = "buyer@example.com", subject: str = "Hello") -> EmailMessage:
    return EmailMessage(to=to, subject=subject, text="secret body with a ticket link")


# --- after commit only ---


def test_email_is_sent_only_after_the_commit(
    db_session: Session, emails: RecordingEmailProvider
) -> None:
    create_user(db_session)
    notifications.send_after_commit(db_session, message())

    assert emails.sent == []  # flushed rows, but no commit yet
    db_session.commit()
    assert [m.to for m in emails.sent] == ["buyer@example.com"]

    db_session.commit()  # a later commit does not send it again
    assert len(emails.sent) == 1
    assert notifications.queued(db_session) == []


def test_rollback_drops_the_queue(db_session: Session, emails: RecordingEmailProvider) -> None:
    notifications.send_after_commit(db_session, message())

    db_session.rollback()
    db_session.commit()

    assert emails.sent == []


def test_closing_without_commit_drops_the_queue(emails: RecordingEmailProvider) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        notifications.send_after_commit(session, message())
        session.close()  # e.g. the request failed before the commit
        assert notifications.queued(session) == []
        transaction.rollback()

    assert emails.sent == []


def test_several_emails_of_one_commit_all_go(
    db_session: Session, emails: RecordingEmailProvider
) -> None:
    for n in range(3):
        notifications.send_after_commit(db_session, message(to=f"b{n}@example.com"))

    db_session.commit()

    assert [m.to for m in emails.sent] == ["b0@example.com", "b1@example.com", "b2@example.com"]


# --- failures ---


class FailingFor(RecordingEmailProvider):
    """Raises for one recipient, records the others."""

    def __init__(self, bad: str) -> None:
        super().__init__()
        self.bad = bad

    def send(self, message: EmailMessage) -> None:
        if message.to == self.bad:
            raise ConnectionRefusedError("mail server down")
        super().send(message)


def test_provider_failure_is_logged_and_never_breaks_the_action(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    provider = FailingFor("bad@failing.example")
    notifications.set_provider(provider)
    user = create_user(db_session)
    notifications.send_after_commit(db_session, message(to="first@example.com"))
    notifications.send_after_commit(db_session, message(to="bad@failing.example", subject="S"))
    notifications.send_after_commit(db_session, message(to="last@example.com"))

    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        db_session.commit()  # must not raise

    assert db_session.scalar(select(User).where(User.id == user.id)) is not None  # committed
    assert [m.to for m in provider.sent] == ["first@example.com", "last@example.com"]
    [record] = [r for r in caplog.records if r.name == "app.notifications"]
    assert record.levelno == logging.ERROR
    assert "failing.example" in record.getMessage()
    # Neither the full address nor the body (ticket links, tokens) reach the log.
    assert "bad@failing.example" not in caplog.text
    assert "secret body" not in caplog.text
    assert record.exc_info is not None  # the traceback is kept for debugging


def test_deliver_reports_failure() -> None:
    notifications.set_provider(FailingFor("x@example.com"))

    assert notifications.deliver(message(to="x@example.com")) is False
    assert notifications.deliver(message(to="y@example.com")) is True


# --- SMTP ---


@pytest.fixture
def silent_smtp_server() -> Iterator[int]:
    """A TCP server that accepts connections and never says anything (a hung mail server)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(server.accept()[0])
            except TimeoutError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    for conn in accepted:
        conn.close()
    server.close()


def smtp_settings(**overrides: object) -> Settings:
    return get_settings().model_copy(
        update={"email_provider": "smtp", "smtp_host": "127.0.0.1", **overrides}
    )


def test_smtp_timeout_is_logged_and_does_not_hang(
    db_session: Session, silent_smtp_server: int, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SmtpEmailProvider(smtp_settings(smtp_port=silent_smtp_server, smtp_timeout=0.3))
    # smtplib reports a silent greeting as SMTPServerDisconnected("... timed out").
    started = time.monotonic()
    with pytest.raises(OSError, match="timed out"):
        provider.send(message())
    assert time.monotonic() - started < 2

    notifications.set_provider(provider)
    notifications.send_after_commit(db_session, message())
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        db_session.commit()  # the action succeeds; only the email failed

    assert time.monotonic() - started < 2
    assert "notification email failed" in caplog.text
    assert "timed out" in caplog.text


class FakeSMTP:
    """Stands in for smtplib.SMTP and records what the provider does."""

    calls: list[tuple] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        FakeSMTP.calls.append(("connect", host, port, timeout))

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        FakeSMTP.calls.append(("quit",))

    def starttls(self) -> None:
        FakeSMTP.calls.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, mime) -> None:
        FakeSMTP.calls.append(("send", mime))


def test_smtp_provider_builds_and_sends_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr("app.modules.notifications.providers.smtplib.SMTP", FakeSMTP)
    settings = smtp_settings(
        smtp_port=2525,
        smtp_user="mailer",
        smtp_password="pw",
        smtp_timeout=7.5,
        email_from="ticketoff <no-reply@ticketoff.store>",
    )
    pdf = Attachment("ticket-1.pdf", b"%PDF-1.4 sample")

    SmtpEmailProvider(settings).send(
        EmailMessage(
            to="buyer@example.com",
            subject="Ваш билет",
            text="Текст",
            html="<p>Текст</p>",
            attachments=(pdf,),
        )
    )

    connect, starttls, login, (_, mime), quit_ = FakeSMTP.calls
    assert connect == ("connect", "127.0.0.1", 2525, 7.5)
    assert (starttls, login, quit_) == (("starttls",), ("login", "mailer", "pw"), ("quit",))
    assert (mime["From"], mime["To"], mime["Subject"]) == (
        "ticketoff <no-reply@ticketoff.store>",
        "buyer@example.com",
        "Ваш билет",
    )
    body = mime.get_body(("plain",))
    assert body.get_content().strip() == "Текст"
    assert mime.get_body(("html",)).get_content().strip() == "<p>Текст</p>"
    [attachment] = list(mime.iter_attachments())
    assert (attachment.get_filename(), attachment.get_content()) == ("ticket-1.pdf", pdf.content)


def test_smtp_without_starttls_or_login(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.calls = []
    monkeypatch.setattr("app.modules.notifications.providers.smtplib.SMTP", FakeSMTP)

    SmtpEmailProvider(smtp_settings(smtp_starttls=False, smtp_user=None)).send(message())

    assert [c[0] for c in FakeSMTP.calls] == ["connect", "send", "quit"]


# --- console provider and provider choice ---


def test_console_provider_logs_and_lists_attachments_by_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.notifications"):
        ConsoleEmailProvider().send(
            EmailMessage(
                to="b@example.com",
                subject="Hi",
                text="Body text",
                attachments=(Attachment("t.pdf", b"x" * 42),),
            )
        )

    assert "to=b@example.com" in caplog.text and "Body text" in caplog.text
    assert "t.pdf (42 B)" in caplog.text


def test_console_provider_off_local_logs_no_body_and_no_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.notifications"):
        ConsoleEmailProvider(log_body=False).send(
            EmailMessage(to="b@example.com", subject="Hi", text="Guest name and order link")
        )

    assert "to domain example.com" in caplog.text and "'Hi'" in caplog.text
    assert "b@example.com" not in caplog.text and "Guest name" not in caplog.text


def test_console_provider_never_logs_a_secret_body(caplog: pytest.LogCaptureFixture) -> None:
    secret = EmailMessage(to="b@example.com", subject="Hi", text="token=abc", contains_secret=True)

    with caplog.at_level(logging.INFO, logger="app.notifications"):
        ConsoleEmailProvider(log_body=True).send(secret)

    assert "token=abc" not in caplog.text and "body not logged" in caplog.text


@pytest.mark.parametrize(("environment", "logs_body"), [("local", True), ("production", False)])
def test_console_logs_bodies_only_locally(
    monkeypatch: pytest.MonkeyPatch, environment: str, logs_body: bool
) -> None:
    configured = get_settings().model_copy(update={"environment": environment})
    monkeypatch.setattr(notifications, "get_settings", lambda: configured)
    notifications.set_provider(None)

    provider = notifications.get_provider()

    assert isinstance(provider, ConsoleEmailProvider) and provider._log_body is logs_body
    # BE-37: the confirmation / reset link is logged locally only (no mailbox to open there).
    assert provider._log_secrets is logs_body


def test_console_provider_logs_a_secret_link_locally(caplog: pytest.LogCaptureFixture) -> None:
    secret = EmailMessage(to="b@example.com", subject="Hi", text="token=abc", contains_secret=True)

    with caplog.at_level(logging.INFO, logger="app.notifications"):
        ConsoleEmailProvider(log_body=True, log_secrets=True).send(secret)
        ConsoleEmailProvider(log_body=False, log_secrets=True).send(secret)  # body off wins

    assert caplog.text.count("token=abc") == 1


@pytest.mark.parametrize(
    ("setting", "kind"), [("console", ConsoleEmailProvider), ("smtp", SmtpEmailProvider)]
)
def test_provider_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch, setting: str, kind: type
) -> None:
    configured = smtp_settings(email_provider=setting)
    monkeypatch.setattr(notifications, "get_settings", lambda: configured)
    notifications.set_provider(None)

    assert isinstance(notifications.get_provider(), kind)


def test_smtp_without_host_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="EMAIL_PROVIDER=smtp needs SMTP_HOST"):
        Settings(email_provider="smtp")


def test_defaults_are_console_and_ten_seconds() -> None:
    settings = Settings()
    assert (settings.email_provider, settings.smtp_timeout, settings.smtp_port) == (
        "console",
        10,
        587,
    )
