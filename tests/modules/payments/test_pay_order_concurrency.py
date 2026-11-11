"""Two concurrent pays of one order, on separate real connections (BE-14).

Rows are committed (the threads cannot see a rolled-back test session) and deleted after.
"""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.ops.models import Notification
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus, TicketKind
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate
from app.modules.payments import service
from app.modules.payments.enums import PaymentStatus
from app.modules.payments.models import Payment
from app.modules.payments.schemas import PayOrderIn
from tests.markers import postgres_only

pytestmark = [postgres_only, pytest.mark.slow]

RACERS = 2
QUANTITY = 3


def _create_pending_order(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Commit an activated event and a pending checkout; return (order id, buyer id)."""
    organizer_user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    buyer = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}")
    session.add_all([organizer_user, buyer, venue])
    session.flush()
    organizer = OrganizerProfile(user_id=organizer_user.id, display_name="Organizer")
    session.add(organizer)
    session.flush()
    event = Event(
        organizer_id=organizer.id,
        venue_id=venue.id,
        title="Race",
        status=EventStatus.PUBLISHED,
        paid_sales_active=True,
        start_at=datetime.now(UTC) + timedelta(days=30),
    )
    session.add(event)
    session.flush()
    ticket_type = TicketType(
        event_id=event.id,
        name="Standard",
        kind=TicketKind.PAID,
        price_kzt=15000,
        quantity_total=10,
        max_per_order=10,
    )
    session.add(ticket_type)
    session.commit()

    data = CheckoutCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": QUANTITY}],
            "attendees": [{"name": f"Guest {n}"} for n in range(QUANTITY)],
        }
    )
    order = orders_service.checkout(session, buyer, event.id, data)
    return order.id, buyer.id


@pytest.fixture
def pending_order() -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    with SessionLocal() as session:
        order_id, buyer_id = _create_pending_order(session)
        order = session.get_one(Order, order_id)
        event = session.get_one(Event, order.event_id)
        organizer = session.get_one(OrganizerProfile, event.organizer_id)
        ids = (order_id, buyer_id, event.id, event.venue_id, organizer.id, organizer.user_id)

    yield order_id, buyer_id

    order_id, buyer_id, event_id, venue_id, organizer_id, organizer_user_id = ids
    with SessionLocal() as cleanup:
        item_ids = select(OrderItem.id).where(OrderItem.order_id == order_id)
        cleanup.execute(delete(Notification).where(Notification.user_id == buyer_id))
        cleanup.execute(delete(Payment).where(Payment.order_id == order_id))
        cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        cleanup.execute(delete(Attendee).where(Attendee.order_id == order_id))
        cleanup.execute(delete(OrderItem).where(OrderItem.order_id == order_id))
        cleanup.execute(delete(Order).where(Order.id == order_id))
        cleanup.execute(delete(TicketType).where(TicketType.event_id == event_id))
        cleanup.execute(delete(Event).where(Event.id == event_id))
        cleanup.execute(delete(Venue).where(Venue.id == venue_id))
        cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == organizer_id))
        cleanup.execute(delete(User).where(User.id.in_([buyer_id, organizer_user_id])))
        cleanup.commit()


def test_concurrent_pays_create_one_payment_and_one_set_of_tickets(
    pending_order: tuple[uuid.UUID, uuid.UUID],
) -> None:
    order_id, buyer_id = pending_order
    barrier = threading.Barrier(RACERS)
    results: list[object] = []

    def run() -> None:
        with SessionLocal() as session:
            buyer = session.get_one(User, buyer_id)
            barrier.wait()
            try:
                results.append(
                    service.pay_order(session, buyer, order_id, PayOrderIn(simulate="success"))
                )
            except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Both requests succeed and report the same payment and the same tickets.
    assert len(results) == RACERS
    assert all(not isinstance(r, Exception) for r in results), results
    assert len({r.payment.id for r in results}) == 1
    ticket_sets = [frozenset(t.id for t in r.order.tickets) for r in results]
    assert ticket_sets[0] == ticket_sets[1]
    assert len(ticket_sets[0]) == QUANTITY

    with SessionLocal() as check:
        statuses = check.scalars(select(Payment.status).where(Payment.order_id == order_id)).all()
        item_ids = select(OrderItem.id).where(OrderItem.order_id == order_id)
        stored = check.scalar(
            select(func.count(Ticket.id)).where(Ticket.order_item_id.in_(item_ids))
        )
        notifications = check.scalar(
            select(func.count(Notification.id)).where(Notification.user_id == buyer_id)
        )
        assert statuses == [PaymentStatus.SUCCEEDED]
        assert stored == QUANTITY
        assert notifications == 1
        assert check.get_one(Order, order_id).status == OrderStatus.PAID


def test_idempotent_pay_does_not_return_stale_tickets(
    pending_order: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A session that loaded the order before another session paid it must still return
    the tickets: the lock re-reads the order row, the reload re-reads items and tickets."""
    order_id, buyer_id = pending_order
    with SessionLocal() as stale, SessionLocal() as other:
        stale_buyer = stale.get_one(User, buyer_id)
        assert orders_service.get_my_order(stale, stale_buyer, order_id).tickets == []
        stale.commit()  # end the read transaction; the loaded objects stay in the session

        paid = service.pay_order(
            other, other.get_one(User, buyer_id), order_id, PayOrderIn(simulate="success")
        )

        again = service.pay_order(stale, stale_buyer, order_id, PayOrderIn(simulate="success"))

    assert again.order.status == OrderStatus.PAID
    assert {t.id for t in again.order.tickets} == {t.id for t in paid.order.tickets}
    assert len(again.order.tickets) == QUANTITY
