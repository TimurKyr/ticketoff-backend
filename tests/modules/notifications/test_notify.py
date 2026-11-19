"""Stage 2 of BE-18: notify() writes the in-app row and queues the email together."""

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from jinja2 import UndefinedError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.modules.identity.enums import Language
from app.modules.identity.models import User
from app.modules.notifications import service as notifications
from app.modules.notifications.attachments import MAX_ATTACHED_TICKETS, ticket_files
from app.modules.ops.enums import NotificationChannel, NotificationType, SupportStatus
from app.modules.ops.models import Notification
from app.modules.orders.pdf import TicketPdfData
from tests.emails import RecordingEmailProvider
from tests.factories import create_user
from tests.modules.notifications.test_templates import CONTEXTS

FAILED = NotificationType.PAYMENT_FAILED


def settings_with(**changes: object) -> Settings:
    return get_settings().model_copy(update=changes)


def rows(session: Session, user: User) -> list[Notification]:
    return list(session.scalars(select(Notification).where(Notification.user_id == user.id)))


def user_speaking(session: Session, language: Language) -> User:
    user = create_user(session)
    user.language = language
    session.flush()
    return user


# --- row and email go together ---


def test_row_and_email_both_exist_after_commit(
    db_session: Session, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)
    order_id = str(uuid.uuid4())

    notifications.notify(db_session, user, FAILED, CONTEXTS[FAILED], payload={"order_id": order_id})
    assert emails.sent == []  # queued, not sent, before the commit
    db_session.commit()

    [row] = rows(db_session, user)
    assert row.type == FAILED and row.channel == NotificationChannel.IN_APP
    assert row.payload == {"order_id": order_id} and row.read_at is None
    [email] = emails.sent
    assert email.to == user.email and email.subject == "Оплата не прошла: Джаз в парке"


def test_neither_exists_after_rollback(db_session: Session, emails: RecordingEmailProvider) -> None:
    user = create_user(db_session)
    db_session.commit()

    notifications.notify(db_session, user, FAILED, CONTEXTS[FAILED])
    db_session.rollback()
    db_session.commit()

    assert rows(db_session, user) == [] and emails.sent == []


def test_email_is_in_the_users_language(
    db_session: Session, emails: RecordingEmailProvider
) -> None:
    ru, en = user_speaking(db_session, Language.RU), user_speaking(db_session, Language.EN)

    notifications.notify(db_session, ru, FAILED, CONTEXTS[FAILED])
    notifications.notify(db_session, en, FAILED, CONTEXTS[FAILED])
    db_session.commit()

    assert emails.to(ru.email)[0].subject.startswith("Оплата не прошла")
    assert emails.to(en.email)[0].subject.startswith("Payment failed")


def test_email_only_writes_no_row(db_session: Session, emails: RecordingEmailProvider) -> None:
    user = create_user(db_session)
    verification = NotificationType.ACCOUNT_VERIFICATION

    notifications.notify(db_session, user, verification, CONTEXTS[verification], in_app=False)
    db_session.commit()

    assert rows(db_session, user) == [] and len(emails.sent) == 1


def test_email_is_rendered_before_the_commit(
    db_session: Session, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)
    context = dict(CONTEXTS[FAILED])

    notifications.notify(db_session, user, FAILED, context)
    context["event_title"] = "changed after notify"  # the caller's dict is not read again
    db_session.commit()

    assert "Джаз в парке" in emails.sent[0].text


# --- rendering errors ---


def test_missing_context_fails_loudly_locally(db_session: Session) -> None:
    user = create_user(db_session)
    context = {k: v for k, v in CONTEXTS[FAILED].items() if k != "retry_until"}

    with pytest.raises(UndefinedError):
        notifications.notify(db_session, user, FAILED, context)


def test_outside_local_a_broken_email_is_logged_and_the_row_kept(
    db_session: Session,
    emails: RecordingEmailProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        notifications, "get_settings", lambda: settings_with(environment="production")
    )
    user = create_user(db_session)

    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        notifications.notify(db_session, user, FAILED, {"event_title": "x"})
    db_session.commit()

    assert len(rows(db_session, user)) == 1 and emails.sent == []
    assert "payment_failed not rendered" in caplog.text


# --- PDF attachments ---


def pdf_data(n: int) -> list[TicketPdfData]:
    starts = datetime(2026, 11, 21, 19, 0, tzinfo=ZoneInfo("Asia/Almaty"))
    return [
        TicketPdfData(
            event_title="Джаз в парке",
            starts_at_local=starts,
            tz_name="Asia/Almaty",
            venue_line="Дворец Республики, Алматы",
            ticket_type="Standard",
            guest_name=f"Гость {i}",
            ticket_id=uuid.uuid4(),
            qr_token=f"qr-token-{i}-{uuid.uuid4().hex}",
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("count", [1, MAX_ATTACHED_TICKETS])
def test_up_to_ten_tickets_are_attached_as_pdfs(
    db_session: Session, emails: RecordingEmailProvider, count: int
) -> None:
    tickets = pdf_data(count)
    files = ticket_files(tickets)
    assert files.tickets_url is None

    user = create_user(db_session)
    context = {**CONTEXTS[NotificationType.ORDER_CONFIRMED], "tickets_url": files.tickets_url}
    notifications.notify(
        db_session, user, NotificationType.ORDER_CONFIRMED, context, attachments=files.attachments
    )
    db_session.commit()

    [email] = emails.sent
    assert [a.filename for a in email.attachments] == [f"ticket-{t.ticket_id}.pdf" for t in tickets]
    assert all(a.mime_type == "application/pdf" for a in email.attachments)
    assert all(a.content.startswith(b"%PDF") for a in email.attachments)
    assert "во вложении" in email.text
    for t in tickets:  # the QR token is in the PDF only, never in the email
        assert t.qr_token not in email.text and t.qr_token not in email.html


def test_above_ten_tickets_the_email_links_to_the_account(
    db_session: Session, emails: RecordingEmailProvider
) -> None:
    files = ticket_files(pdf_data(MAX_ATTACHED_TICKETS + 1))
    assert files.attachments == ()
    assert files.tickets_url == "https://ticketoff.store/tickets"

    user = create_user(db_session)
    context = {**CONTEXTS[NotificationType.ORDER_CONFIRMED], "tickets_url": files.tickets_url}
    notifications.notify(
        db_session, user, NotificationType.ORDER_CONFIRMED, context, attachments=files.attachments
    )
    db_session.commit()

    [email] = emails.sent
    assert email.attachments == ()
    assert "Все билеты: https://ticketoff.store/tickets" in email.text


def test_links_follow_frontend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.notifications import links

    monkeypatch.setattr(
        links,
        "get_settings",
        lambda: settings_with(frontend_url="https://staging.ticketoff.store/"),
    )
    order_id = uuid.uuid4()

    assert links.order_url(order_id) == f"https://staging.ticketoff.store/orders/{order_id}"
    assert links.tickets_url() == "https://staging.ticketoff.store/tickets"
    assert links.support_url() == "https://staging.ticketoff.store/account"


# --- support (BE-17) ---


def test_notify_support_reply(db_session: Session, emails: RecordingEmailProvider) -> None:
    user = create_user(db_session)
    case_id = uuid.uuid4()
    body = "Здравствуйте!\n\n" + "Мы отправили билет повторно. " * 20

    notifications.notify_support(
        db_session, user, case_id=case_id, case_subject="Не пришёл билет", message=body
    )
    db_session.commit()

    [row] = rows(db_session, user)
    assert row.type == NotificationType.SUPPORT_REPLY and row.payload == {"case_id": str(case_id)}
    [email] = emails.sent
    assert email.subject == "Ответ поддержки: Не пришёл билет"
    assert "Открыть обращение: https://ticketoff.store/account" in email.text
    excerpt = email.text.split("\n\n")[1]
    assert excerpt.startswith("Здравствуйте! Мы отправили") and excerpt.endswith("…")
    assert len(excerpt) <= notifications.SUPPORT_EXCERPT_CHARS


def test_notify_support_status(db_session: Session, emails: RecordingEmailProvider) -> None:
    user = user_speaking(db_session, Language.EN)

    notifications.notify_support(
        db_session, user, case_id=uuid.uuid4(), case_subject="Refund", status=SupportStatus.RESOLVED
    )
    db_session.commit()

    assert rows(db_session, user)[0].type == NotificationType.SUPPORT_STATUS
    assert 'The status of your request "Refund" changed: resolved.' in emails.sent[0].text


@pytest.mark.parametrize("kwargs", [{}, {"message": "hi", "status": SupportStatus.OPEN}])
def test_notify_support_needs_exactly_one_of_message_or_status(
    db_session: Session, kwargs: dict
) -> None:
    user = create_user(db_session)

    with pytest.raises(ValueError, match="exactly one"):
        notifications.notify_support(
            db_session, user, case_id=uuid.uuid4(), case_subject="x", **kwargs
        )
    assert rows(db_session, user) == []
