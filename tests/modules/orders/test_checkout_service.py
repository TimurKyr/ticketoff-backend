"""Paid checkout (BE-14), service level: pending order, inventory hold, no tickets yet."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.orders import service
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Attendee, Order, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate, RegistrationCreate
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_order_item,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

# FOR UPDATE locks and timezone-aware datetime comparisons.
pytestmark = postgres_only

HOLD = timedelta(minutes=15)


@pytest.fixture
def event(db_session: Session) -> Event:
    """A published event with paid sales activated."""
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


def _body(*items: tuple[TicketType, int]) -> dict:
    total = sum(q for _, q in items)
    return {
        "items": [{"ticket_type_id": str(tt.id), "quantity": q} for tt, q in items],
        "attendees": [{"name": f"Guest {n}", "email": None} for n in range(total)],
    }


def checkout(session: Session, buyer: User, event: Event, *items: tuple[TicketType, int]):
    return service.checkout(session, buyer, event.id, CheckoutCreate.model_validate(_body(*items)))


def _expire(session: Session, order_id) -> None:
    """Make an order older than the checkout hold."""
    session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(created_at=datetime.now(UTC) - HOLD - timedelta(minutes=1))
    )
    session.expire_all()


# --- happy path ---


def test_creates_pending_order_with_db_prices_and_no_tickets(
    db_session: Session, buyer: User, event: Event, paid_type: TicketType
) -> None:
    student = create_ticket_type(db_session, event, name="Student", price_kzt=7000)

    out = checkout(db_session, buyer, event, (paid_type, 2), (student, 1))

    assert out.status == OrderStatus.PENDING
    assert out.total_kzt == 2 * 15000 + 7000
    assert {(i.ticket_type_id, i.unit_price_kzt) for i in out.items} == {
        (paid_type.id, 15000),
        (student.id, 7000),
    }
    assert out.tickets == []
    order = db_session.get(Order, out.id)
    assert order.discount_kzt == 0
    assert order.buyer_user_id == buyer.id
    item_ids = [item.id for item in order.items]
    stored = db_session.scalar(
        select(func.count(Ticket.id)).where(Ticket.order_item_id.in_(item_ids))
    )
    assert stored == 0


def test_attendees_are_linked_to_their_items(
    db_session: Session, buyer: User, event: Event, paid_type: TicketType
) -> None:
    student = create_ticket_type(db_session, event, name="Student", price_kzt=7000)

    out = checkout(db_session, buyer, event, (paid_type, 1), (student, 2))

    attendees = db_session.scalars(select(Attendee).where(Attendee.order_id == out.id)).all()
    by_type = sorted(a.order_item.ticket_type_id == student.id for a in attendees)
    assert by_type == [False, True, True]


def test_expires_at_is_created_at_plus_hold(
    db_session: Session, buyer: User, event: Event, paid_type: TicketType
) -> None:
    out = checkout(db_session, buyer, event, (paid_type, 1))

    created_at = db_session.get(Order, out.id).created_at
    assert out.expires_at == created_at + HOLD
    # Visible on the buyer's reads too.
    assert service.get_my_order(db_session, buyer, out.id).expires_at == out.expires_at
    [listed] = service.list_my_orders(db_session, buyer)
    assert listed.expires_at == out.expires_at


def test_non_pending_orders_have_no_expires_at(
    db_session: Session, buyer: User, event: Event
) -> None:
    free_type = create_free_ticket_type(db_session, event)
    registration = RegistrationCreate.model_validate(_body((free_type, 1)))

    out = service.register_free(db_session, buyer, event.id, registration)

    assert out.expires_at is None
    assert service.get_my_order(db_session, buyer, out.id).expires_at is None


# --- activation ---


def test_paid_before_activation_is_409(
    db_session: Session, buyer: User, event: Event, paid_type: TicketType
) -> None:
    event.paid_sales_active = False
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="Paid ticket sales are not active"):
        checkout(db_session, buyer, event, (paid_type, 1))


def test_activation_check_comes_before_sold_out(
    db_session: Session, buyer: User, event: Event
) -> None:
    sold_out = create_ticket_type(db_session, event, quantity_total=1, max_per_order=1)
    create_order_item(db_session, sold_out, 1, OrderStatus.PAID)
    event.paid_sales_active = False
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="Paid ticket sales are not active"):
        checkout(db_session, buyer, event, (sold_out, 1))


# --- ticket type rules ---


def test_free_type_is_409(db_session: Session, buyer: User, event: Event) -> None:
    free_type = create_free_ticket_type(db_session, event)

    with pytest.raises(BusinessRuleError, match="Use registration for free tickets"):
        checkout(db_session, buyer, event, (free_type, 1))


def test_hidden_type_is_404(db_session: Session, buyer: User, event: Event) -> None:
    hidden = create_ticket_type(db_session, event, is_hidden=True)

    with pytest.raises(NotFoundError):
        checkout(db_session, buyer, event, (hidden, 1))


def test_other_events_type_is_404(db_session: Session, buyer: User, event: Event) -> None:
    other_event = create_event(db_session, create_organizer(db_session))
    other_type = create_ticket_type(db_session, other_event)

    with pytest.raises(NotFoundError):
        checkout(db_session, buyer, event, (other_type, 1))


@pytest.mark.parametrize(
    ("window", "message"),
    [
        ({"sales_start_at": datetime.now(UTC) + timedelta(days=1)}, "have not started"),
        ({"sales_end_at": datetime.now(UTC) - timedelta(days=1)}, "have ended"),
    ],
)
def test_outside_sales_window_is_409(
    db_session: Session, buyer: User, event: Event, window: dict, message: str
) -> None:
    ticket_type = create_ticket_type(db_session, event, **window)

    with pytest.raises(BusinessRuleError, match=message):
        checkout(db_session, buyer, event, (ticket_type, 1))


def test_over_max_per_order_is_409(db_session: Session, buyer: User, event: Event) -> None:
    ticket_type = create_ticket_type(db_session, event, max_per_order=2)

    with pytest.raises(BusinessRuleError, match="At most 2"):
        checkout(db_session, buyer, event, (ticket_type, 3))


def test_sold_out_is_409(db_session: Session, buyer: User, event: Event) -> None:
    ticket_type = create_ticket_type(db_session, event, quantity_total=2, max_per_order=2)
    create_order_item(db_session, ticket_type, 2, OrderStatus.PAID)

    with pytest.raises(BusinessRuleError, match="Not enough tickets left"):
        checkout(db_session, buyer, event, (ticket_type, 1))


# --- inventory hold ---


def test_fresh_pending_order_holds_stock_and_expired_one_does_not(
    db_session: Session, buyer: User, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, quantity_total=2, max_per_order=2)
    first = checkout(db_session, buyer, event, (ticket_type, 2))

    with pytest.raises(BusinessRuleError, match="Not enough tickets left"):
        checkout(db_session, create_user(db_session), event, (ticket_type, 1))

    _expire(db_session, first.id)
    second = checkout(db_session, create_user(db_session), event, (ticket_type, 2))
    assert second.status == OrderStatus.PENDING


def test_pending_checkout_blocks_free_registration_over_capacity(
    db_session: Session, buyer: User, event: Event, paid_type: TicketType
) -> None:
    """BE-13 regression: free registration counts fresh pending orders, not expired ones."""
    event.capacity = 3
    free_type = create_free_ticket_type(db_session, event)
    db_session.flush()
    pending = checkout(db_session, buyer, event, (paid_type, 2))
    registration = RegistrationCreate.model_validate(_body((free_type, 2)))

    with pytest.raises(BusinessRuleError, match="Not enough capacity left"):
        service.register_free(db_session, create_user(db_session), event.id, registration)

    _expire(db_session, pending.id)
    out = service.register_free(db_session, create_user(db_session), event.id, registration)
    assert out.status == OrderStatus.FREE_CONFIRMED
    assert len(out.tickets) == 2
