"""Stage 3 of BE-18: each flow tells the buyer, inside its own transaction.

Per wiring point: the right recipient, type, content (ru and en) and attachments; nothing
when the action fails or its commit fails; a failing provider never breaks the action; the
attendees named on an order are never emailed.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError
from app.core.db import engine
from app.modules.events import service as events_service
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import EventUpdate
from app.modules.identity.enums import Language
from app.modules.identity.models import OrganizerProfile, User
from app.modules.notifications import flows
from app.modules.notifications import service as notifications
from app.modules.notifications.attachments import MAX_ATTACHED_TICKETS
from app.modules.notifications.formatting import local_datetime
from app.modules.notifications.providers import EmailMessage
from app.modules.ops.enums import NotificationChannel, NotificationType
from app.modules.ops.models import Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order, TicketType
from app.modules.orders.schemas import CheckoutCreate, OrderOut, RegistrationCreate
from app.modules.payments import service as payments_service
from app.modules.payments.schemas import PayOrderIn, RefundIn
from tests.emails import RecordingEmailProvider
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

pytestmark = postgres_only

PRICE = 5000
ALMATY = "Asia/Almaty"  # the factory venue's time zone
SUCCESS, FAIL = PayOrderIn(simulate="success"), PayOrderIn(simulate="fail")
T = NotificationType


def plain(text: str) -> str:
    return text.replace(" ", " ")  # amounts use no-break spaces


def order_link(order_id: uuid.UUID) -> str:
    return f"https://ticketoff.store/orders/{order_id}"


# --- fixtures ---


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def owner(db_session: Session, organizer: OrganizerProfile) -> User:
    return db_session.get_one(User, organizer.user_id)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    event = create_event(db_session, organizer)
    event.title = "Джаз в парке"
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, name="VIP", price_kzt=PRICE, quantity_total=50)


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(
        db_session, event, name="Вход", quantity_total=50, max_per_order=20
    )


def buyer(session: Session, language: Language = Language.RU) -> User:
    user = create_user(session)
    user.language = language
    session.flush()
    return user


def body(ticket_type: TicketType, quantity: int) -> dict:
    # Attendees have their own addresses: they must never get an email.
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": quantity}],
        "attendees": [
            {"name": f"Гость {n}", "email": f"attendee{n}@example.com"} for n in range(quantity)
        ],
    }


def register(session: Session, user: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    data = RegistrationCreate.model_validate(body(ticket_type, quantity))
    return orders_service.register_free(session, user, ticket_type.event_id, data)


def checkout(session: Session, user: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    data = CheckoutCreate.model_validate(body(ticket_type, quantity))
    return orders_service.checkout(session, user, ticket_type.event_id, data)


def buy(session: Session, user: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    pending = checkout(session, user, ticket_type, quantity)
    return payments_service.pay_order(session, user, pending.id, SUCCESS).order


def rows(session: Session, user: User, type_: NotificationType | None = None) -> list:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if type_ is not None:
        stmt = stmt.where(Notification.type == type_)
    return list(session.scalars(stmt))


def only_buyers_emailed(emails: RecordingEmailProvider) -> None:
    assert not [m.to for m in emails.sent if m.to.startswith("attendee")]


class FailingCommit(Exception):
    pass


def fail_next_commit(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The action's commit fails (e.g. a serialization error); the request then rolls back."""

    def commit() -> None:
        raise FailingCommit

    monkeypatch.setattr(session, "commit", commit)


def rolled_back(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    session.rollback()
    session.commit()  # a later, unrelated commit must not send the dropped emails


class BrokenProvider:
    def send(self, message: EmailMessage) -> None:
        raise ConnectionRefusedError("smtp down")


@pytest.fixture
def broken_provider(caplog: pytest.LogCaptureFixture):
    notifications.set_provider(BrokenProvider())
    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        yield caplog
    notifications.set_provider(None)


# --- free registration: order_confirmed with PDFs ---


def test_free_registration_emails_the_buyer_with_pdfs(
    db_session: Session, emails: RecordingEmailProvider, free_type: TicketType
) -> None:
    user = buyer(db_session)

    order = register(db_session, user, free_type, 2)

    [row] = rows(db_session, user)
    assert (row.type, row.channel, row.payload) == (
        T.ORDER_CONFIRMED,
        NotificationChannel.IN_APP,
        {"order_id": str(order.id)},
    )
    [email] = emails.sent
    assert email.to == user.email
    assert email.subject == "Регистрация подтверждена: Джаз в парке"
    assert "- Вход: Гость 0" in email.text and "- Вход: Гость 1" in email.text
    assert "₸" not in email.text  # free: no amounts
    assert f"Заказ: {order_link(order.id)}" in email.text
    calendar = f"https://api.ticketoff.store/events/{order.event_id}/calendar.ics"
    assert f"Добавить в календарь (.ics): {calendar}" in email.text
    assert sorted(a.filename for a in email.attachments) == sorted(
        f"ticket-{t.id}.pdf" for t in order.tickets
    )
    for ticket in order.tickets:  # the QR token is only inside the PDF
        assert ticket.qr_token not in email.text and ticket.qr_token not in email.html
    only_buyers_emailed(emails)


def test_free_registration_in_english(
    db_session: Session, emails: RecordingEmailProvider, free_type: TicketType
) -> None:
    register(db_session, buyer(db_session, Language.EN), free_type, 1)

    [email] = emails.sent
    assert email.subject == "Registration confirmed: Джаз в парке"
    assert "Your tickets are attached" in email.text


@pytest.mark.parametrize("quantity", [MAX_ATTACHED_TICKETS, MAX_ATTACHED_TICKETS + 1])
def test_free_registration_attaches_up_to_ten_pdfs(
    db_session: Session, emails: RecordingEmailProvider, free_type: TicketType, quantity: int
) -> None:
    register(db_session, buyer(db_session), free_type, quantity)

    [email] = emails.sent
    if quantity <= MAX_ATTACHED_TICKETS:
        assert len(email.attachments) == quantity
        assert "Все билеты" not in email.text
    else:
        assert email.attachments == ()
        assert "Все билеты: https://ticketoff.store/tickets" in email.text


def test_refused_registration_sends_nothing(
    db_session: Session, emails: RecordingEmailProvider, free_type: TicketType
) -> None:
    user = buyer(db_session)

    with pytest.raises(BusinessRuleError, match="At most 20"):
        register(db_session, user, free_type, 21)  # max_per_order is 20
    db_session.rollback()
    db_session.commit()

    assert rows(db_session, user) == [] and emails.sent == []


def test_registration_whose_commit_fails_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    free_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = buyer(db_session)
    db_session.commit()
    fail_next_commit(db_session, monkeypatch)

    with pytest.raises(FailingCommit):
        register(db_session, user, free_type, 1)
    rolled_back(db_session, monkeypatch)

    assert rows(db_session, user) == [] and emails.sent == []


def test_registration_commits_even_if_the_mail_server_is_down(
    db_session: Session, free_type: TicketType, broken_provider: pytest.LogCaptureFixture
) -> None:
    user = buyer(db_session)

    order = register(db_session, user, free_type, 1)

    assert db_session.get_one(Order, order.id).status == order.status
    assert len(rows(db_session, user)) == 1
    assert "notification email failed" in broken_provider.text


# --- pay success: order_confirmed with PDFs, same payload as before ---


def test_paid_order_emails_the_buyer_with_amounts_and_pdfs(
    db_session: Session, emails: RecordingEmailProvider, paid_type: TicketType
) -> None:
    user = buyer(db_session)
    pending = checkout(db_session, user, paid_type, 2)
    assert emails.sent == []  # a checkout alone tells nobody

    order = payments_service.pay_order(db_session, user, pending.id, SUCCESS).order

    [row] = rows(db_session, user)
    assert (row.type, row.payload) == (T.ORDER_CONFIRMED, {"order_id": str(order.id)})
    [email] = emails.sent
    assert email.to == user.email
    assert email.subject == "Заказ оплачен: Джаз в парке"
    assert "Сумма: 10 000 ₸" in plain(email.text) and "Оплачено: 10 000 ₸" in plain(email.text)
    assert "- VIP: Гость 0" in email.text
    assert f"Заказ: {order_link(order.id)}" in email.text
    assert [a.filename for a in email.attachments] == [f"ticket-{t.id}.pdf" for t in order.tickets]
    only_buyers_emailed(emails)


def test_paid_order_in_english(
    db_session: Session, emails: RecordingEmailProvider, paid_type: TicketType
) -> None:
    user = buyer(db_session, Language.EN)
    buy(db_session, user, paid_type, 1)

    [email] = emails.sent
    assert email.subject == "Order paid: Джаз в парке"
    assert "Paid: 5 000 ₸" in plain(email.text)


def test_paying_twice_sends_one_email(
    db_session: Session, emails: RecordingEmailProvider, paid_type: TicketType
) -> None:
    user = buyer(db_session)
    pending = checkout(db_session, user, paid_type, 1)
    payments_service.pay_order(db_session, user, pending.id, SUCCESS)

    payments_service.pay_order(db_session, user, pending.id, SUCCESS)  # retry: already paid

    assert len(emails.sent) == 1 and len(rows(db_session, user)) == 1


def test_pay_whose_commit_fails_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    paid_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = buyer(db_session)
    pending = checkout(db_session, user, paid_type, 1)
    fail_next_commit(db_session, monkeypatch)

    with pytest.raises(FailingCommit):
        payments_service.pay_order(db_session, user, pending.id, SUCCESS)
    rolled_back(db_session, monkeypatch)

    assert rows(db_session, user) == [] and emails.sent == []


def test_expired_checkout_pay_sends_nothing(
    db_session: Session, emails: RecordingEmailProvider, paid_type: TicketType
) -> None:
    user = buyer(db_session)
    pending = checkout(db_session, user, paid_type, 1)
    expired = orders_repository.checkout_hold() + timedelta(minutes=1)
    db_session.get_one(Order, pending.id).created_at = datetime.now(UTC) - expired
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="expired"):
        payments_service.pay_order(db_session, user, pending.id, SUCCESS)

    assert rows(db_session, user) == [] and emails.sent == []


def test_pay_commits_even_if_the_mail_server_is_down(
    db_session: Session, paid_type: TicketType, broken_provider: pytest.LogCaptureFixture
) -> None:
    user = buyer(db_session)

    order = buy(db_session, user, paid_type, 1)

    assert order.status.value == "paid" and len(rows(db_session, user)) == 1
    assert "notification email failed" in broken_provider.text


# --- pay fail: payment_failed ---


def test_failed_payment_tells_the_buyer_until_when_to_retry(
    db_session: Session, emails: RecordingEmailProvider, paid_type: TicketType
) -> None:
    user = buyer(db_session)
    pending = checkout(db_session, user, paid_type, 2)

    result = payments_service.pay_order(db_session, user, pending.id, FAIL)

    [row] = rows(db_session, user)
    assert (row.type, row.payload) == (
        T.PAYMENT_FAILED,
        {"order_id": str(pending.id), "payment_id": str(result.payment.id)},
    )
    [email] = emails.sent
    assert email.to == user.email and email.attachments == ()
    assert email.subject == "Оплата не прошла: Джаз в парке"
    assert "Сумма: 10 000 ₸" in plain(email.text)
    retry_until = local_datetime(pending.expires_at, ALMATY, "ru")
    assert f"Повторить оплату можно до {retry_until}." in email.text
    assert f"Заказ: {order_link(pending.id)}" in email.text


def test_failed_payment_in_english(
    db_session: Session, emails: RecordingEmailProvider, paid_type: TicketType
) -> None:
    user = buyer(db_session, Language.EN)
    pending = checkout(db_session, user, paid_type, 1)

    payments_service.pay_order(db_session, user, pending.id, FAIL)

    [email] = emails.sent
    assert email.subject == "Payment failed: Джаз в парке"
    assert "You can retry until" in email.text and "Amount: 5 000 ₸" in plain(email.text)


def test_failed_payment_whose_commit_fails_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    paid_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = buyer(db_session)
    pending = checkout(db_session, user, paid_type, 1)
    fail_next_commit(db_session, monkeypatch)

    with pytest.raises(FailingCommit):
        payments_service.pay_order(db_session, user, pending.id, FAIL)
    rolled_back(db_session, monkeypatch)

    assert rows(db_session, user) == [] and emails.sent == []


# --- single refund: refund_processed, same payload as before ---


def test_refund_emails_the_buyer(
    db_session: Session, emails: RecordingEmailProvider, owner: User, paid_type: TicketType
) -> None:
    user = buyer(db_session)
    order = buy(db_session, user, paid_type, 2)
    emails.sent.clear()

    result = payments_service.refund_order(
        db_session, owner, order.id, RefundIn(reason="<b>Сломалась сцена</b>")
    )

    [row] = rows(db_session, user, T.REFUND_PROCESSED)
    assert row.payload == {
        "order_id": str(order.id),
        "refund_id": str(result.refund.id),
        "amount_kzt": 2 * PRICE,
    }
    [email] = emails.sent
    assert email.to == user.email  # the buyer, never the organizer who refunded
    assert email.subject == "Возврат оформлен: Джаз в парке"
    assert "Сумма возврата: 10 000 ₸" in plain(email.text)
    assert "Причина: <b>Сломалась сцена</b>" in email.text
    assert "&lt;b&gt;Сломалась сцена&lt;/b&gt;" in email.html  # typed by the organizer
    assert f"Заказ: {order_link(order.id)}" in email.text


def test_refund_in_english(
    db_session: Session, emails: RecordingEmailProvider, owner: User, paid_type: TicketType
) -> None:
    order = buy(db_session, buyer(db_session, Language.EN), paid_type, 1)
    emails.sent.clear()

    payments_service.refund_order(db_session, owner, order.id, RefundIn())

    [email] = emails.sent
    assert email.subject == "Refund issued: Джаз в парке"
    assert "Refund: 5 000 ₸" in plain(email.text) and "Reason" not in email.text


def test_refused_refund_sends_nothing(
    db_session: Session, emails: RecordingEmailProvider, owner: User, paid_type: TicketType
) -> None:
    user = buyer(db_session)
    order = buy(db_session, user, paid_type, 1)
    payments_service.refund_order(db_session, owner, order.id, RefundIn())
    emails.sent.clear()

    with pytest.raises(BusinessRuleError, match="already refunded"):
        payments_service.refund_order(db_session, owner, order.id, RefundIn())

    assert emails.sent == [] and len(rows(db_session, user, T.REFUND_PROCESSED)) == 1


def test_refund_whose_commit_fails_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    paid_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = buyer(db_session)
    order = buy(db_session, user, paid_type, 1)
    emails.sent.clear()
    fail_next_commit(db_session, monkeypatch)

    with pytest.raises(FailingCommit):
        payments_service.refund_order(db_session, owner, order.id, RefundIn())
    rolled_back(db_session, monkeypatch)

    assert rows(db_session, user, T.REFUND_PROCESSED) == [] and emails.sent == []


def test_refund_commits_even_if_the_mail_server_is_down(
    db_session: Session, owner: User, paid_type: TicketType, caplog: pytest.LogCaptureFixture
) -> None:
    user = buyer(db_session)
    order = buy(db_session, user, paid_type, 1)
    notifications.set_provider(BrokenProvider())

    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        result = payments_service.refund_order(db_session, owner, order.id, RefundIn())

    assert result.order.status.value == "refunded"
    assert len(rows(db_session, user, T.REFUND_PROCESSED)) == 1
    assert "notification email failed" in caplog.text


# --- event cancel cascade: event_cancelled, one per affected buyer ---


def test_event_cancel_emails_each_affected_buyer_once(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    paid_type: TicketType,
    free_type: TicketType,
) -> None:
    both = buyer(db_session)  # a free registration and two paid orders
    free_only = buyer(db_session, Language.EN)
    pending_only = buyer(db_session)
    untouched = buyer(db_session)  # only an order refunded earlier
    free = register(db_session, both, free_type, 1)
    paid_a = buy(db_session, both, paid_type, 2)
    paid_b = buy(db_session, both, paid_type, 1)
    register(db_session, free_only, free_type, 3)
    pending = checkout(db_session, pending_only, paid_type, 1)
    earlier = buy(db_session, untouched, paid_type, 1)
    payments_service.refund_order(db_session, owner, earlier.id, RefundIn())
    emails.sent.clear()

    payments_service.cancel_event_with_orders(db_session, event, owner)

    assert sorted(m.to for m in emails.sent) == sorted(
        [both.email, free_only.email, pending_only.email]
    )
    assert rows(db_session, untouched, T.EVENT_CANCELLED) == []
    assert rows(db_session, owner) == []  # the organizer is not a buyer here
    only_buyers_emailed(emails)

    # One email for all three orders, with everything refunded to this buyer.
    [row] = rows(db_session, both, T.EVENT_CANCELLED)
    assert row.payload["event_id"] == str(event.id) and row.payload["refund_kzt"] == 3 * PRICE
    assert sorted(row.payload["order_ids"]) == sorted(str(o.id) for o in (free, paid_a, paid_b))
    [email] = emails.to(both.email)
    assert email.subject == "Событие отменено: Джаз в парке"
    assert "Мы полностью вернули оплату: 15 000 ₸." in plain(email.text)
    for o in (free, paid_a, paid_b):
        assert f"Заказ: {order_link(o.id)}" in email.text
    assert f"https://api.ticketoff.store/events/{event.id}/calendar.ics" in email.text

    # Free registration only, in English: no refund line.
    [email] = emails.to(free_only.email)
    assert email.subject == "Event cancelled: Джаз в парке"
    assert "refunded" not in email.text and "₸" not in email.text
    assert rows(db_session, free_only, T.EVENT_CANCELLED)[0].payload["refund_kzt"] == 0

    # A pending checkout the cascade cancelled: told, nothing refunded.
    [email] = emails.to(pending_only.email)
    assert "вернули" not in email.text and order_link(pending.id) in email.text


def test_event_cancel_whose_commit_fails_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    paid_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = buyer(db_session)
    buy(db_session, user, paid_type, 1)
    emails.sent.clear()
    fail_next_commit(db_session, monkeypatch)

    with pytest.raises(FailingCommit):
        payments_service.cancel_event_with_orders(db_session, event, owner)
    rolled_back(db_session, monkeypatch)

    assert rows(db_session, user, T.EVENT_CANCELLED) == [] and emails.sent == []


def test_refused_event_cancel_sends_nothing(
    db_session: Session, emails: RecordingEmailProvider, owner: User, event: Event
) -> None:
    event.status = EventStatus.COMPLETED
    db_session.flush()

    with pytest.raises(BusinessRuleError):
        payments_service.cancel_event_with_orders(db_session, event, owner)

    assert emails.sent == []


def test_event_cancel_commits_even_if_the_mail_server_is_down(
    db_session: Session,
    owner: User,
    event: Event,
    paid_type: TicketType,
    broken_provider: pytest.LogCaptureFixture,
) -> None:
    user = buyer(db_session)
    buy(db_session, user, paid_type, 1)

    out = payments_service.cancel_event_with_orders(db_session, event, owner)

    assert out.status == EventStatus.CANCELLED
    assert len(rows(db_session, user, T.EVENT_CANCELLED)) == 1
    assert "notification email failed" in broken_provider.text


# --- event update: event_updated to ticket holders ---


@pytest.fixture
def holders(
    db_session: Session, event: Event, paid_type: TicketType, free_type: TicketType
) -> dict[str, User]:
    """Who holds what on the event; only paid and free_confirmed orders count."""
    users = {
        "paid": buyer(db_session),
        "free_en": buyer(db_session, Language.EN),
        "two_orders": buyer(db_session),
        "pending": buyer(db_session),
        "cancelled": buyer(db_session),
    }
    buy(db_session, users["paid"], paid_type, 1)
    register(db_session, users["free_en"], free_type, 1)
    buy(db_session, users["two_orders"], paid_type, 1)
    register(db_session, users["two_orders"], free_type, 1)
    checkout(db_session, users["pending"], paid_type, 1)
    gone = register(db_session, users["cancelled"], free_type, 1)
    orders_service.cancel_order(db_session, users["cancelled"], gone.id)
    return users


def test_moving_a_published_event_tells_each_holder_once(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    holders: dict[str, User],
) -> None:
    old_start = event.start_at
    new_start = old_start + timedelta(days=7, hours=1)
    new_end = new_start + timedelta(hours=3)
    emails.sent.clear()

    events_service.update_event(
        db_session, event, EventUpdate(start_at=new_start, end_at=new_end), owner
    )

    told = [holders["paid"], holders["free_en"], holders["two_orders"]]
    assert sorted(m.to for m in emails.sent) == sorted(u.email for u in told)
    for name in ("pending", "cancelled"):
        assert rows(db_session, holders[name], T.EVENT_UPDATED) == []
    assert rows(db_session, owner) == []
    only_buyers_emailed(emails)

    [row] = rows(db_session, holders["two_orders"], T.EVENT_UPDATED)
    assert row.payload == {"event_id": str(event.id), "fields": ["starts_at", "ends_at"]}
    [email] = emails.to(holders["paid"].email)
    assert email.subject == "Изменения в событии: Джаз в парке"
    old, new = local_datetime(old_start, ALMATY, "ru"), local_datetime(new_start, ALMATY, "ru")
    assert f"Начало: {old} → {new}" in email.text
    assert "Окончание: не указано → " in email.text
    [email] = emails.to(holders["free_en"].email)
    assert email.subject == "Event changed: Джаз в парке"
    assert "End: not set → " in email.text


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Новое название"},
        {"description": "Новое описание"},
        {"capacity": 500},
    ],
)
def test_other_fields_tell_nobody(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    holders: dict[str, User],
    change: dict,
) -> None:
    emails.sent.clear()

    events_service.update_event(db_session, event, EventUpdate(**change), owner)

    assert emails.sent == []
    assert rows(db_session, holders["paid"], T.EVENT_UPDATED) == []


def test_sending_the_same_start_tells_nobody(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    holders: dict[str, User],
) -> None:
    emails.sent.clear()

    events_service.update_event(db_session, event, EventUpdate(start_at=event.start_at), owner)

    assert emails.sent == []


def test_moving_a_draft_tells_nobody(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    holders: dict[str, User],
) -> None:
    event.status = EventStatus.DRAFT
    db_session.flush()
    emails.sent.clear()

    events_service.update_event(
        db_session, event, EventUpdate(start_at=event.start_at + timedelta(days=1)), owner
    )

    assert emails.sent == []
    assert rows(db_session, holders["paid"], T.EVENT_UPDATED) == []


def test_venue_change_is_announced_with_both_venues(
    db_session: Session, emails: RecordingEmailProvider, event: Event, holders: dict[str, User]
) -> None:
    # update_event refuses a venue change once tickets are held (_LOCKED_WITH_ORDERS), so the
    # flow is exercised directly: it is ready if that rule is ever relaxed.
    new_venue = Venue(name="Almaty Arena", address="Момышулы 44", city="Алматы")
    db_session.add(new_venue)
    db_session.flush()
    before = flows.schedule_of(event)
    event.venue_id = new_venue.id
    emails.sent.clear()

    flows.event_updated(db_session, event, before)
    db_session.commit()

    [email] = emails.to(holders["paid"].email)
    assert "Место: Hall → Almaty Arena, Момышулы 44, Алматы" in email.text
    [row] = rows(db_session, holders["paid"], T.EVENT_UPDATED)
    assert row.payload["fields"] == ["venue"]


def test_event_update_whose_commit_fails_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    holders: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emails.sent.clear()
    fail_next_commit(db_session, monkeypatch)

    with pytest.raises(FailingCommit):
        events_service.update_event(
            db_session, event, EventUpdate(start_at=event.start_at + timedelta(days=1)), owner
        )
    rolled_back(db_session, monkeypatch)

    assert emails.sent == []
    assert rows(db_session, holders["paid"], T.EVENT_UPDATED) == []


def test_refused_event_update_sends_nothing(
    db_session: Session,
    emails: RecordingEmailProvider,
    owner: User,
    event: Event,
    holders: dict[str, User],
) -> None:
    emails.sent.clear()

    with pytest.raises(BusinessRuleError, match="past"):
        events_service.update_event(
            db_session, event, EventUpdate(start_at=datetime.now(UTC) - timedelta(days=1)), owner
        )

    assert emails.sent == []


def test_event_update_commits_even_if_the_mail_server_is_down(
    db_session: Session,
    owner: User,
    event: Event,
    holders: dict[str, User],
    broken_provider: pytest.LogCaptureFixture,
) -> None:
    new_start = event.start_at + timedelta(days=1)

    events_service.update_event(db_session, event, EventUpdate(start_at=new_start), owner)

    assert db_session.get_one(Event, event.id).start_at == new_start
    assert len(rows(db_session, holders["paid"], T.EVENT_UPDATED)) == 1
    assert "notification email failed" in broken_provider.text


# --- event cancel: no query per buyer ---


def _cancel_statements(db_session: Session, owner: User, organizer, buyers: int) -> int:
    """SQL statements run by cancelling an event whose `buyers` buyers each hold one free
    registration and one pending checkout (the orders the cascade changes and notifies)."""
    event = create_event(db_session, organizer)
    event.paid_sales_active = True
    free = create_free_ticket_type(db_session, event, quantity_total=100)
    paid = create_ticket_type(db_session, event, price_kzt=PRICE, quantity_total=100)
    for _ in range(buyers):
        user = buyer(db_session)
        register(db_session, user, free, 1)
        checkout(db_session, user, paid, 1)
    db_session.expire_all()  # start cold, as a fresh request would

    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        payments_service.cancel_event_with_orders(db_session, event, owner)
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert len(rows_for_event(db_session, event)) == buyers
    return len(statements)


def rows_for_event(session: Session, event: Event) -> list[Notification]:
    stmt = select(Notification).where(
        Notification.type == T.EVENT_CANCELLED,
        Notification.payload["event_id"].astext == str(event.id),
    )
    return list(session.scalars(stmt))


def test_event_cancel_queries_do_not_grow_with_buyers(
    db_session: Session, emails: RecordingEmailProvider, owner: User, organizer: OrganizerProfile
) -> None:
    few = _cancel_statements(db_session, owner, organizer, buyers=2)
    many = _cancel_statements(db_session, owner, organizer, buyers=10)

    assert many == few, f"{few} statements for 2 buyers, {many} for 10"
    cancelled = [m for m in emails.sent if m.subject.startswith("Событие отменено")]
    assert len(cancelled) == 2 + 10  # still one email per buyer
