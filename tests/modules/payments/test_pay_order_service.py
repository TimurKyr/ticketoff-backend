"""Paying a checkout order (BE-14), service level. All payments are simulated."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.ops.enums import NotificationChannel, NotificationType
from app.modules.ops.models import Notification
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, Ticket, TicketType
from app.modules.orders.qr import verify_ticket_token
from app.modules.orders.schemas import CheckoutCreate, OrderOut
from app.modules.payments import service
from app.modules.payments.enums import PaymentPurpose, PaymentStatus
from app.modules.payments.models import Payment
from app.modules.payments.schemas import PayOrderIn
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

pytestmark = postgres_only

SUCCESS = PayOrderIn(simulate="success")
FAIL = PayOrderIn(simulate="fail")
AFTER_HOLD = timedelta(minutes=16)


@pytest.fixture
def event(db_session: Session) -> Event:
    event = create_event(db_session, create_organizer(db_session))
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def buyer(db_session: Session) -> User:
    return create_user(db_session)


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, price_kzt=15000, quantity_total=10)


@pytest.fixture
def pending(db_session: Session, buyer: User, event: Event, paid_type: TicketType) -> OrderOut:
    """A fresh pending checkout for 2 tickets (30 000 KZT)."""
    data = CheckoutCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(paid_type.id), "quantity": 2}],
            "attendees": [{"name": "Aruzhan"}, {"name": "Dias"}],
        }
    )
    return orders_service.checkout(db_session, buyer, event.id, data)


def _payments(session: Session, order_id: uuid.UUID) -> list[Payment]:
    return list(session.scalars(select(Payment).where(Payment.order_id == order_id)))


def _notifications(session: Session, user: User) -> list[Notification]:
    return list(session.scalars(select(Notification).where(Notification.user_id == user.id)))


def _stored_tickets(session: Session, order_id: uuid.UUID) -> int:
    order = session.get(Order, order_id)
    item_ids = [item.id for item in order.items]
    return session.scalar(select(func.count(Ticket.id)).where(Ticket.order_item_id.in_(item_ids)))


# --- success ---


def test_success_pays_order_and_issues_signed_tickets(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    result = service.pay_order(db_session, buyer, pending.id, SUCCESS)

    assert result.payment.status == PaymentStatus.SUCCEEDED
    assert result.payment.is_simulated is True
    assert result.payment.amount_kzt == 30000
    assert result.order.status == OrderStatus.PAID
    assert result.order.expires_at is None
    assert len(result.order.tickets) == 2
    assert sorted(t.attendee.name for t in result.order.tickets) == ["Aruzhan", "Dias"]
    for ticket in result.order.tickets:
        assert verify_ticket_token(ticket.qr_token) == ticket.id

    [payment] = _payments(db_session, pending.id)
    assert (payment.purpose, payment.status, payment.is_simulated) == (
        PaymentPurpose.TICKET_ORDER,
        PaymentStatus.SUCCEEDED,
        True,
    )
    assert payment.event_id is None
    assert _stored_tickets(db_session, pending.id) == 2


def test_success_creates_one_in_app_notification(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    service.pay_order(db_session, buyer, pending.id, SUCCESS)

    [notification] = _notifications(db_session, buyer)
    assert notification.type == NotificationType.ORDER_CONFIRMED
    assert notification.channel == NotificationChannel.IN_APP
    assert notification.payload == {"order_id": str(pending.id)}
    assert notification.read_at is None


def test_response_shows_tickets_even_if_order_was_loaded_before(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    """Items with an empty tickets collection already in the session must not leak into
    the response."""
    assert orders_service.get_my_order(db_session, buyer, pending.id).tickets == []

    result = service.pay_order(db_session, buyer, pending.id, SUCCESS)

    assert len(result.order.tickets) == 2
    assert orders_service.get_my_order(db_session, buyer, pending.id).tickets == (
        result.order.tickets
    )


def test_pay_twice_returns_the_same_tickets(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    first = service.pay_order(db_session, buyer, pending.id, SUCCESS)
    db_session.expire_all()

    second = service.pay_order(db_session, buyer, pending.id, SUCCESS)

    assert second.payment.id == first.payment.id
    assert {t.id for t in second.order.tickets} == {t.id for t in first.order.tickets}
    assert [p.status for p in _payments(db_session, pending.id)] == [PaymentStatus.SUCCEEDED]
    assert _stored_tickets(db_session, pending.id) == 2
    assert len(_notifications(db_session, buyer)) == 1


def test_fail_after_success_returns_the_paid_result(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    """A paid order is final: a later "fail" attempt changes nothing."""
    first = service.pay_order(db_session, buyer, pending.id, SUCCESS)

    again = service.pay_order(db_session, buyer, pending.id, FAIL)

    assert again.payment.id == first.payment.id
    assert again.order.status == OrderStatus.PAID
    assert len(_payments(db_session, pending.id)) == 1


# --- fail ---


def test_fail_records_failed_payment_and_keeps_order_pending(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    result = service.pay_order(db_session, buyer, pending.id, FAIL)

    assert result.payment.status == PaymentStatus.FAILED
    assert result.payment.is_simulated is True
    assert result.order.status == OrderStatus.PENDING
    assert result.order.tickets == []
    assert result.order.expires_at == pending.expires_at
    assert _stored_tickets(db_session, pending.id) == 0
    assert _notifications(db_session, buyer) == []


def test_success_after_fail_works(db_session: Session, buyer: User, pending: OrderOut) -> None:
    service.pay_order(db_session, buyer, pending.id, FAIL)

    result = service.pay_order(db_session, buyer, pending.id, SUCCESS)

    assert result.order.status == OrderStatus.PAID
    assert len(result.order.tickets) == 2
    assert sorted(p.status for p in _payments(db_session, pending.id)) == [
        PaymentStatus.FAILED,
        PaymentStatus.SUCCEEDED,
    ]


# --- rejections ---


def test_expired_order_is_cancelled_and_409(
    db_session: Session, buyer: User, pending: OrderOut
) -> None:
    later = datetime.now(UTC) + AFTER_HOLD

    with pytest.raises(BusinessRuleError, match="Checkout expired, start again"):
        service.pay_order(db_session, buyer, pending.id, SUCCESS, now=later)

    db_session.expire_all()
    assert db_session.get(Order, pending.id).status == OrderStatus.CANCELLED
    assert _payments(db_session, pending.id) == []
    # Cancelled for good: a retry is refused too.
    with pytest.raises(BusinessRuleError, match="Order cannot be paid"):
        service.pay_order(db_session, buyer, pending.id, SUCCESS)


def test_other_users_order_is_404(db_session: Session, pending: OrderOut) -> None:
    with pytest.raises(NotFoundError, match="Order not found"):
        service.pay_order(db_session, create_user(db_session), pending.id, SUCCESS)

    assert _payments(db_session, pending.id) == []


def test_unknown_order_is_404(db_session: Session, buyer: User) -> None:
    with pytest.raises(NotFoundError):
        service.pay_order(db_session, buyer, uuid.uuid4(), SUCCESS)


def test_free_order_is_409(db_session: Session, buyer: User, event: Event) -> None:
    free_type = create_free_ticket_type(db_session, event)
    free = orders_service.register_free(
        db_session,
        buyer,
        event.id,
        CheckoutCreate.model_validate(
            {
                "items": [{"ticket_type_id": str(free_type.id), "quantity": 1}],
                "attendees": [{"name": "A"}],
            }
        ),
    )

    with pytest.raises(BusinessRuleError, match="Order cannot be paid"):
        service.pay_order(db_session, buyer, free.id, SUCCESS)


def test_cancelled_order_is_409(db_session: Session, buyer: User, pending: OrderOut) -> None:
    db_session.get(Order, pending.id).status = OrderStatus.CANCELLED
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="Order cannot be paid"):
        service.pay_order(db_session, buyer, pending.id, SUCCESS)


@pytest.mark.parametrize(
    "change",
    [
        {"paid_sales_active": False},
        {"status": EventStatus.CANCELLED},
        {"status": EventStatus.DRAFT},
    ],
)
def test_paid_sales_off_after_checkout_is_409(
    db_session: Session, buyer: User, event: Event, pending: OrderOut, change: dict
) -> None:
    for field, value in change.items():
        setattr(event, field, value)
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="Paid ticket sales are not active"):
        service.pay_order(db_session, buyer, pending.id, SUCCESS)

    assert _payments(db_session, pending.id) == []
    assert db_session.get(Order, pending.id).status == OrderStatus.PENDING
