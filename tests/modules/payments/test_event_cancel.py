"""POST /events/{event_id}/cancel cascade (BE-19 PR C): the event's orders are cancelled
and paid ones refunded, in one transaction with one audit row."""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.db import SessionLocal, engine, get_session
from app.main import app
from app.modules.audit import service as audit_service
from app.modules.audit.models import AuditLog
from app.modules.checkin import service as checkin_service
from app.modules.checkin.schemas import ScanResult
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.enums import NotificationType
from app.modules.ops.models import CheckInRecord, Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate, OrderOut, RegistrationCreate
from app.modules.payments import service
from app.modules.payments.models import Payment, Refund
from app.modules.payments.schemas import PayOrderIn, RefundIn
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


def _body(ticket_type: TicketType, quantity: int) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": quantity}],
        "attendees": [{"name": f"Guest {n}"} for n in range(quantity)],
    }


def register(session: Session, buyer: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    data = RegistrationCreate.model_validate(_body(ticket_type, quantity))
    return orders_service.register_free(session, buyer, ticket_type.event_id, data)


def checkout(session: Session, buyer: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    data = CheckoutCreate.model_validate(_body(ticket_type, quantity))
    return orders_service.checkout(session, buyer, ticket_type.event_id, data)


def buy(session: Session, buyer: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    pending = checkout(session, buyer, ticket_type, quantity)
    return service.pay_order(session, buyer, pending.id, PayOrderIn(simulate="success")).order


@dataclass
class Stocked:
    free: OrderOut
    paid_two: OrderOut  # one of its tickets is checked in
    paid_one: OrderOut
    pending_fresh: OrderOut
    pending_expired: OrderOut
    already_cancelled: OrderOut
    already_refunded: OrderOut


def stock(
    session: Session, owner: User, event: Event, paid_type: TicketType, free_type: TicketType
) -> Stocked:
    """One order in every state, each from its own buyer."""
    buyer = lambda: create_user(session)  # noqa: E731
    s = Stocked(
        free=register(session, buyer(), free_type, 2),
        paid_two=buy(session, buyer(), paid_type, 2),
        paid_one=buy(session, buyer(), paid_type, 1),
        pending_fresh=checkout(session, buyer(), paid_type, 1),
        pending_expired=checkout(session, buyer(), paid_type, 1),
        already_cancelled=register(session, (b := buyer()), free_type, 1),
        already_refunded=buy(session, buyer(), paid_type, 1),
    )
    orders_service.cancel_order(session, b, s.already_cancelled.id)
    service.refund_order(session, owner, s.already_refunded.id, RefundIn(reason="Earlier"))
    session.get_one(Ticket, s.paid_two.tickets[0].id).status = TicketStatus.CHECKED_IN
    expired = orders_repository.checkout_hold(session) + timedelta(minutes=1)
    session.get_one(Order, s.pending_expired.id).created_at = datetime.now(UTC) - expired
    session.flush()
    return s


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def owner(db_session: Session, organizer: OrganizerProfile) -> User:
    return db_session.get_one(User, organizer.user_id)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    event = create_event(db_session, organizer)
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, price_kzt=PRICE, quantity_total=10)


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event, quantity_total=10)


@pytest.fixture
def stocked(
    db_session: Session, owner: User, event: Event, paid_type: TicketType, free_type: TicketType
) -> Stocked:
    return stock(db_session, owner, event, paid_type, free_type)


def _order(session: Session, order: OrderOut) -> tuple[OrderStatus, list[TicketStatus]]:
    stored = orders_repository.get_order_with_details(session, order.id, reload=True)
    return stored.status, sorted(t.status for item in stored.items for t in item.tickets)


def _refunds(session: Session, order: OrderOut) -> list[Refund]:
    stmt = select(Refund).join(Payment, Payment.id == Refund.payment_id)
    return list(session.scalars(stmt.where(Payment.order_id == order.id)))


def _count(session: Session, model: type, *where: object) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where))


# --- the cascade ---


def test_cancel_cascades_to_every_order(
    db_session: Session,
    owner: User,
    event: Event,
    paid_type: TicketType,
    free_type: TicketType,
    stocked: Stocked,
) -> None:
    audit_before = _count(db_session, AuditLog, AuditLog.event_id == event.id)
    notifications_before = _count(db_session, Notification)

    out = service.cancel_event_with_orders(db_session, event, owner)

    assert out.status == EventStatus.CANCELLED
    cancelled, refunded = TicketStatus.CANCELLED, TicketStatus.REFUNDED
    assert _order(db_session, stocked.free) == (OrderStatus.CANCELLED, [cancelled] * 2)
    # The checked-in ticket is refunded too.
    assert _order(db_session, stocked.paid_two) == (OrderStatus.REFUNDED, [refunded] * 2)
    assert _order(db_session, stocked.paid_one) == (OrderStatus.REFUNDED, [refunded])
    assert _order(db_session, stocked.pending_fresh) == (OrderStatus.CANCELLED, [])
    assert _order(db_session, stocked.pending_expired) == (OrderStatus.CANCELLED, [])
    assert _order(db_session, stocked.already_cancelled) == (OrderStatus.CANCELLED, [cancelled])
    assert _order(db_session, stocked.already_refunded) == (OrderStatus.REFUNDED, [refunded])

    # One full refund per paid order, with the reason; the earlier refund is not repeated.
    for order, amount in ((stocked.paid_two, 2 * PRICE), (stocked.paid_one, PRICE)):
        [refund] = _refunds(db_session, order)
        assert (refund.amount_kzt, refund.reason) == (amount, "Event cancelled")
    assert [r.reason for r in _refunds(db_session, stocked.already_refunded)] == ["Earlier"]

    # Exactly one new audit row, the event's, with exact counts; no per-order rows.
    # Picked by action_type, not by position: the whole test runs in one transaction, so every
    # row has the same created_at (now()) and ordering by it is random.
    assert _count(db_session, AuditLog, AuditLog.event_id == event.id) == audit_before + 1
    [row] = db_session.scalars(
        select(AuditLog).where(
            AuditLog.event_id == event.id, AuditLog.action_type == "event.cancelled"
        )
    ).all()
    assert (row.action_type, row.entity_type, row.entity_id, row.actor_user_id) == (
        "event.cancelled",
        "event",
        event.id,
        owner.id,
    )
    assert row.details == {
        "cancelled_orders": 1,
        "refunded_orders": 2,
        "refunded_kzt": 3 * PRICE,
        "pending_cancelled": 2,
        "ticket_count": 5,  # 2 free + 2 + 1 paid
    }
    # BE-18: one event_cancelled per buyer whose order the cascade changed (free, both paid,
    # both pending: 5 buyers here), none for the already cancelled/refunded orders. Their
    # content is tested in tests/modules/notifications/test_wiring.py.
    assert _count(db_session, Notification) == notifications_before + 5
    for_event = Notification.payload["event_id"].astext == str(event.id)
    cancelled_type = Notification.type == NotificationType.EVENT_CANCELLED
    assert _count(db_session, Notification, for_event, cancelled_type) == 5

    # Inventory back to full, refunds equal everything that was paid.
    available = orders_service.get_availability(db_session, [paid_type, free_type])
    assert available == {paid_type.id: 10, free_type.id: 10}
    summary = service.get_summary(db_session, event)
    assert (summary.gross_kzt, summary.refunds_kzt) == (4 * PRICE, 4 * PRICE)


def test_cancel_after_the_start_still_refunds(
    db_session: Session, owner: User, event: Event, paid_type: TicketType
) -> None:
    paid = buy(db_session, create_user(db_session), paid_type, 1)
    event.start_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.flush()

    service.cancel_event_with_orders(db_session, event, owner)

    assert _order(db_session, paid) == (OrderStatus.REFUNDED, [TicketStatus.REFUNDED])


def test_draft_without_orders_cancels(
    db_session: Session, owner: User, organizer: OrganizerProfile
) -> None:
    draft = create_event(db_session, organizer, status=EventStatus.DRAFT)

    out = service.cancel_event_with_orders(db_session, draft, owner)

    assert out.status == EventStatus.CANCELLED
    [row] = db_session.scalars(select(AuditLog).where(AuditLog.event_id == draft.id)).all()
    assert row.details == {
        "cancelled_orders": 0,
        "refunded_orders": 0,
        "refunded_kzt": 0,
        "pending_cancelled": 0,
        "ticket_count": 0,
    }


def test_cancelling_twice_is_409_and_changes_nothing(
    db_session: Session, owner: User, event: Event, stocked: Stocked
) -> None:
    service.cancel_event_with_orders(db_session, event, owner)
    refunds = _count(db_session, Refund)
    audits = _count(db_session, AuditLog, AuditLog.event_id == event.id)

    with pytest.raises(BusinessRuleError, match="^A cancelled event cannot be cancelled$"):
        service.cancel_event_with_orders(db_session, event, owner)

    db_session.rollback()  # the refused attempt's lock; nothing was written
    assert _count(db_session, Refund) == refunds
    assert _count(db_session, AuditLog, AuditLog.event_id == event.id) == audits


def test_locks_event_then_orders_then_tickets(
    db_session: Session, owner: User, event: Event, stocked: Stocked, monkeypatch
) -> None:
    calls: list[str] = []
    for name in ("lock_event", "lock_event_orders", "lock_event_tickets"):
        real = getattr(orders_repository, name)

        def spy(*args: object, _real=real, _name=name) -> object:
            calls.append(_name)
            return _real(*args)

        monkeypatch.setattr(orders_repository, name, spy)

    service.cancel_event_with_orders(db_session, event, owner)

    assert calls == ["lock_event", "lock_event_orders", "lock_event_tickets"]


# --- API ---


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def test_api_cancel_returns_the_event_and_scanner_refuses(
    api: TestClient, owner: User, event: Event, stocked: Stocked
) -> None:
    response = api.post(f"/events/{event.id}/cancel", headers=_headers(owner))

    assert response.status_code == 200
    assert (response.json()["id"], response.json()["status"]) == (str(event.id), "cancelled")
    for ticket in (stocked.free.tickets[0], stocked.paid_one.tickets[0]):
        scan = api.post(
            "/checkin/scan",
            json={"event_id": str(event.id), "qr_token": ticket.qr_token},
            headers=_headers(owner),
        )
        assert scan.json()["result"] == "event_cancelled"


def test_api_twice_is_409(api: TestClient, owner: User, event: Event) -> None:
    assert api.post(f"/events/{event.id}/cancel", headers=_headers(owner)).status_code == 200

    response = api.post(f"/events/{event.id}/cancel", headers=_headers(owner))

    assert response.status_code == 409
    assert response.json() == {"detail": "A cancelled event cannot be cancelled"}


def test_api_another_organizer_gets_404_and_nothing_changes(
    api: TestClient, db_session: Session, event: Event, stocked: Stocked
) -> None:
    stranger = db_session.get_one(User, create_organizer(db_session).user_id)

    response = api.post(f"/events/{event.id}/cancel", headers=_headers(stranger))

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
    assert db_session.get(Event, event.id).status == EventStatus.PUBLISHED
    assert _order(db_session, stocked.paid_one)[0] == OrderStatus.PAID


def test_api_requires_login(api: TestClient, event: Event) -> None:
    assert api.post(f"/events/{event.id}/cancel").status_code == 401


# --- races (real connections, committed rows) ---


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


@dataclass
class Committed:
    event_id: uuid.UUID
    owner_id: uuid.UUID
    paid_type: TicketType
    free_type: TicketType
    buyer_ids: list[uuid.UUID]
    cleanup: dict


def _committed_event() -> Committed:
    with SessionLocal() as s:
        organizer = create_organizer(s)
        event = create_event(s, organizer)
        event.paid_sales_active = True
        paid_type = create_ticket_type(s, event, price_kzt=PRICE, quantity_total=10)
        free_type = create_free_ticket_type(s, event, quantity_total=10)
        buyers = [create_user(s) for _ in range(3)]
        s.commit()
        return Committed(
            event.id,
            organizer.user_id,
            paid_type,
            free_type,
            [b.id for b in buyers],
            {"venue": event.venue_id, "organizer": organizer.id},
        )


def _cleanup(c: Committed) -> None:
    with SessionLocal() as s:
        order_ids = select(Order.id).where(Order.event_id == c.event_id)
        item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        ticket_ids = select(Ticket.id).where(Ticket.order_item_id.in_(item_ids))
        payment_ids = select(Payment.id).where(
            or_(Payment.order_id.in_(order_ids), Payment.event_id == c.event_id)
        )
        users = [c.owner_id, *c.buyer_ids]
        s.execute(delete(CheckInRecord).where(CheckInRecord.ticket_id.in_(ticket_ids)))
        s.execute(delete(Notification).where(Notification.user_id.in_(users)))
        s.execute(delete(AuditLog).where(AuditLog.event_id == c.event_id))
        s.execute(delete(Refund).where(Refund.payment_id.in_(payment_ids)))
        s.execute(delete(Payment).where(Payment.id.in_(payment_ids)))
        s.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        s.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
        s.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        s.execute(delete(Order).where(Order.event_id == c.event_id))
        s.execute(delete(TicketType).where(TicketType.event_id == c.event_id))
        s.execute(delete(Event).where(Event.id == c.event_id))
        s.execute(delete(Venue).where(Venue.id == c.cleanup["venue"]))
        s.execute(delete(OrganizerProfile).where(OrganizerProfile.id == c.cleanup["organizer"]))
        s.execute(delete(User).where(User.id.in_(users)))
        s.commit()


def _pause_before_audit(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Pause the cascade after it has taken all its locks (inside audit.log, before commit)."""
    paused, release = threading.Event(), threading.Event()
    real_log = audit_service.log

    def paused_log(*args: object, **kwargs: object) -> object:
        if kwargs.get("action_type") == "event.cancelled":
            paused.set()
            assert release.wait(10)
        return real_log(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log", paused_log)
    return paused, release


def _cancel_in_thread(c: Committed, results: dict) -> threading.Thread:
    def run() -> None:
        with SessionLocal() as s:
            event = s.get_one(Event, c.event_id)
            service.cancel_event_with_orders(s, event, s.get_one(User, c.owner_id))
            results["cancel"] = "cancelled"

    return threading.Thread(target=run)


@pytest.mark.slow
def test_checkout_and_pay_waiting_behind_a_cancel_are_refused(monkeypatch) -> None:
    """While the cancel holds its locks, a checkout and a pay arrive. Both wait for the
    event lock, then see the cancelled event: no new order, no paid order left behind."""
    c = _committed_event()
    try:
        with SessionLocal() as s:
            pending = checkout(s, s.get_one(User, c.buyer_ids[0]), c.paid_type, 1)  # commits
        paused, release = _pause_before_audit(monkeypatch)
        results: dict[str, object] = {}

        def pay() -> None:
            with SessionLocal() as s:
                try:
                    service.pay_order(
                        s,
                        s.get_one(User, c.buyer_ids[0]),
                        pending.id,
                        PayOrderIn(simulate="success"),
                    )
                    results["pay"] = "paid"
                except (BusinessRuleError, NotFoundError) as exc:
                    results["pay"] = str(exc)

        def new_checkout() -> None:
            with SessionLocal() as s:
                try:
                    checkout(s, s.get_one(User, c.buyer_ids[1]), c.paid_type, 1)
                    results["checkout"] = "created"
                except NotFoundError as exc:
                    results["checkout"] = f"404 {exc}"
                except BusinessRuleError as exc:
                    results["checkout"] = f"409 {exc}"

        canceller = _cancel_in_thread(c, results)
        canceller.start()
        assert paused.wait(10), "cancel never reached audit.log"
        threads = [threading.Thread(target=pay), threading.Thread(target=new_checkout)]
        for thread in threads:
            thread.start()
        _wait_until(lambda: _lock_waiters() >= 2 or not any(t.is_alive() for t in threads))
        release.set()
        for thread in (canceller, *threads):
            thread.join(timeout=30)

        assert results == {
            "cancel": "cancelled",
            "pay": "Order cannot be paid",
            "checkout": "404 Event not found",
        }
        with SessionLocal() as s:
            statuses = s.scalars(select(Order.status).where(Order.event_id == c.event_id)).all()
        assert statuses == [OrderStatus.CANCELLED]  # the pending one; no new order, none paid
    finally:
        _cleanup(c)


@pytest.mark.slow
def test_scan_during_an_event_cancel_admits_nobody(monkeypatch) -> None:
    """A scan arriving while the cancel holds its locks waits for the ticket lock and then
    sees the ticket cancelled: nobody is admitted and no check-in record is written."""
    c = _committed_event()
    try:
        with SessionLocal() as s:
            free = register(s, s.get_one(User, c.buyer_ids[2]), c.free_type, 1)  # commits
        token = free.tickets[0].qr_token
        paused, release = _pause_before_audit(monkeypatch)
        results: dict[str, object] = {}

        def scan() -> None:
            with SessionLocal() as s:
                owner = s.get_one(User, c.owner_id)
                results["scan"] = checkin_service.scan(s, owner, c.event_id, token).result

        canceller = _cancel_in_thread(c, results)
        canceller.start()
        assert paused.wait(10), "cancel never reached audit.log"
        scanner = threading.Thread(target=scan)
        scanner.start()
        _wait_until(lambda: _lock_waiters() >= 1 or not scanner.is_alive())
        release.set()
        canceller.join(timeout=30)
        scanner.join(timeout=30)

        assert results["cancel"] == "cancelled"
        assert results["scan"] != ScanResult.CHECKED_IN
        with SessionLocal() as s:
            ticket = s.scalars(select(Ticket).where(Ticket.qr_token == token)).one()
            assert ticket.status == TicketStatus.CANCELLED
            records = select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)
            assert s.scalars(records).all() == []
    finally:
        _cleanup(c)
