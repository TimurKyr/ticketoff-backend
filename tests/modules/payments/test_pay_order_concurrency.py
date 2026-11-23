"""Two concurrent pays of one order, on separate real connections (BE-14).

Rows are committed (the threads cannot see a rolled-back test session) and deleted after.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError
from app.core.db import SessionLocal, engine
from app.modules.audit.models import AuditLog
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.ops.models import Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus, TicketKind
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate
from app.modules.payments import repository as payments_repository
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
        # Test rows only: audit_logs is append-only in the app, but these were made here.
        cleanup.execute(delete(AuditLog).where(AuditLog.event_id == event_id))
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


# --- the last ticket: checkout vs checkout, and pay vs checkout at the hold boundary ---


@dataclass
class LastTicket:
    event_id: uuid.UUID
    ticket_type_id: uuid.UUID
    buyer_ids: list[uuid.UUID]


@pytest.fixture
def last_ticket() -> Iterator[LastTicket]:
    """A committed activated event with one paid ticket left (quantity_total = 1)."""
    with SessionLocal() as session:
        organizer_user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        buyers = [User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x") for _ in "ab"]
        venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}")
        session.add_all([organizer_user, *buyers, venue])
        session.flush()
        organizer = OrganizerProfile(user_id=organizer_user.id, display_name="Organizer")
        session.add(organizer)
        session.flush()
        event = Event(
            organizer_id=organizer.id,
            venue_id=venue.id,
            title="Last ticket",
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
            quantity_total=1,
            max_per_order=1,
        )
        session.add(ticket_type)
        session.commit()
        fixture = LastTicket(event.id, ticket_type.id, [b.id for b in buyers])
        cleanup_ids = (venue.id, organizer.id, organizer_user.id)

    yield fixture

    venue_id, organizer_id, organizer_user_id = cleanup_ids
    with SessionLocal() as cleanup:
        order_ids = select(Order.id).where(Order.event_id == fixture.event_id)
        item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        cleanup.execute(delete(AuditLog).where(AuditLog.event_id == fixture.event_id))
        cleanup.execute(delete(Notification).where(Notification.user_id.in_(fixture.buyer_ids)))
        cleanup.execute(delete(Payment).where(Payment.order_id.in_(order_ids)))
        cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
        cleanup.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        cleanup.execute(delete(Order).where(Order.event_id == fixture.event_id))
        cleanup.execute(delete(TicketType).where(TicketType.event_id == fixture.event_id))
        cleanup.execute(delete(Event).where(Event.id == fixture.event_id))
        cleanup.execute(delete(Venue).where(Venue.id == venue_id))
        cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == organizer_id))
        cleanup.execute(delete(User).where(User.id.in_([organizer_user_id, *fixture.buyer_ids])))
        cleanup.commit()


def _checkout_one(buyer_id: uuid.UUID, last: LastTicket, before=None) -> str:
    """Check out the last ticket in a session of its own; returns "won" or "rejected"."""
    data = CheckoutCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(last.ticket_type_id), "quantity": 1}],
            "attendees": [{"name": "Guest"}],
        }
    )
    with SessionLocal() as session:
        buyer = session.get_one(User, buyer_id)
        if before:
            before()
        try:
            orders_service.checkout(session, buyer, last.event_id, data)
        except BusinessRuleError:
            return "rejected"
        return "won"


def _held_at(event_id: uuid.UUID, now: datetime) -> int:
    with SessionLocal() as check:
        return orders_repository.held_quantity_for_event(check, event_id, now)


def test_concurrent_checkouts_for_the_last_ticket_one_wins(last_ticket: LastTicket) -> None:
    barrier = threading.Barrier(2)
    results: list[str] = []

    def run(buyer_id: uuid.UUID) -> None:
        results.append(_checkout_one(buyer_id, last_ticket, before=barrier.wait))

    threads = [threading.Thread(target=run, args=(b,)) for b in last_ticket.buyer_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == ["rejected", "won"]
    assert _held_at(last_ticket.event_id, datetime.now(UTC)) == 1


class _FakeClock:
    """datetime.now() for both services, moved by the test (rows still use the DB clock)."""

    value: datetime

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch, start: datetime) -> type["_FakeClock"]:
        cls.value = start

        class FakeDatetime(datetime):
            @classmethod
            def now(cls_, tz=None):  # noqa: N805 - mirrors datetime.now
                return cls.value

        monkeypatch.setattr(orders_service, "datetime", FakeDatetime)
        monkeypatch.setattr(service, "datetime", FakeDatetime)
        return cls


def _lock_waiters() -> int:
    with engine.connect() as conn:
        return conn.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )


def _wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_pay_at_the_hold_boundary_never_oversells(
    last_ticket: LastTicket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pay has passed its expiry check (just before the hold ends) but not committed yet,
    when the clock passes the hold and a new checkout for the same last ticket counts.

    The checkout must not see the order as expired and free while that pay is in flight:
    with the event lock it waits, then sees the order paid and is rejected. Without the
    event lock it would win too, and the one ticket would be sold twice.
    """
    first_buyer, second_buyer = last_ticket.buyer_ids
    assert _checkout_one(first_buyer, last_ticket) == "won"
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    with SessionLocal() as session:
        order = session.scalars(select(Order).where(Order.event_id == last_ticket.event_id)).one()
        order.created_at = created_at  # a known start, so the hold boundary is exact
        order_id = order.id
        session.commit()
        hold = orders_repository.checkout_hold(session)
    expires_at = created_at + hold
    clock = _FakeClock.install(monkeypatch, start=expires_at - timedelta(seconds=1))

    # Pause pay after every check (clock read, not expired) and before its commit.
    paused, release = threading.Event(), threading.Event()
    real_add_payment = payments_repository.add_payment

    def paused_add_payment(*args: object) -> object:
        paused.set()
        assert release.wait(10)
        return real_add_payment(*args)

    monkeypatch.setattr(payments_repository, "add_payment", paused_add_payment)
    results: dict[str, object] = {}

    def pay() -> None:
        with SessionLocal() as session:
            buyer = session.get_one(User, first_buyer)
            try:
                service.pay_order(session, buyer, order_id, PayOrderIn(simulate="success"))
                results["pay"] = "paid"
            except BusinessRuleError as exc:
                results["pay"] = str(exc)

    def checkout() -> None:
        results["checkout"] = _checkout_one(second_buyer, last_ticket)

    payer = threading.Thread(target=pay)
    payer.start()
    assert paused.wait(10), "pay never reached add_payment"

    clock.value = expires_at + timedelta(seconds=1)  # the hold is over for the checkout
    buyer = threading.Thread(target=checkout)
    buyer.start()
    # Either the checkout finishes (nothing stops it) or it waits for pay's event lock.
    _wait_until(lambda: not buyer.is_alive() or _lock_waiters() > 0)

    release.set()
    payer.join(timeout=30)
    buyer.join(timeout=30)

    assert results == {"pay": "paid", "checkout": "rejected"}
    with SessionLocal() as check:
        statuses = check.scalars(
            select(Order.status).where(Order.event_id == last_ticket.event_id)
        ).all()
    assert statuses == [OrderStatus.PAID]  # the rejected checkout created no order
    assert _held_at(last_ticket.event_id, clock.value) == 1


def test_checkout_that_counted_an_expired_order_wins_over_its_late_pay(
    last_ticket: LastTicket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the boundary: a checkout already counted the order as expired and
    holds the event lock; a pay for that order arriving now must see it expired."""
    first_buyer, second_buyer = last_ticket.buyer_ids
    assert _checkout_one(first_buyer, last_ticket) == "won"
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    with SessionLocal() as session:
        order = session.scalars(select(Order).where(Order.event_id == last_ticket.event_id)).one()
        order.created_at = created_at
        order_id = order.id
        session.commit()
        hold = orders_repository.checkout_hold(session)
    expires_at = created_at + hold
    _FakeClock.install(monkeypatch, start=expires_at + timedelta(seconds=1))

    # Pause the checkout after it counted (order expired) and before its commit.
    paused, release = threading.Event(), threading.Event()
    real_add_order = orders_repository.add_order

    def paused_add_order(*args: object) -> None:
        paused.set()
        assert release.wait(10)
        real_add_order(*args)

    monkeypatch.setattr(orders_repository, "add_order", paused_add_order)
    results: dict[str, object] = {}

    def checkout() -> None:
        results["checkout"] = _checkout_one(second_buyer, last_ticket)

    def pay() -> None:
        with SessionLocal() as session:
            buyer = session.get_one(User, first_buyer)
            try:
                service.pay_order(session, buyer, order_id, PayOrderIn(simulate="success"))
                results["pay"] = "paid"
            except BusinessRuleError as exc:
                results["pay"] = str(exc)

    buyer = threading.Thread(target=checkout)
    buyer.start()
    assert paused.wait(10), "checkout never reached add_order"
    payer = threading.Thread(target=pay)
    payer.start()
    _wait_until(lambda: not payer.is_alive() or _lock_waiters() > 0)

    release.set()
    buyer.join(timeout=30)
    payer.join(timeout=30)

    assert results == {"checkout": "won", "pay": "Checkout expired, start again"}
    with SessionLocal() as check:
        statuses = sorted(
            check.scalars(select(Order.status).where(Order.event_id == last_ticket.event_id))
        )
    assert statuses == sorted([OrderStatus.CANCELLED, OrderStatus.PENDING])
