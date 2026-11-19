"""POST /orders/{order_id}/cancel (BE-19): cancel a free registration or a pending checkout."""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.db import SessionLocal, engine, get_session
from app.main import app
from app.modules.audit import service as audit_service
from app.modules.audit.models import AuditLog
from app.modules.checkin import service as checkin_service
from app.modules.checkin.schemas import ScanResult
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.models import CheckInRecord, Notification
from app.modules.orders import repository, service
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate, OrderOut, RegistrationCreate
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_order_item,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

# FOR UPDATE locks and timezone-aware datetimes.
pytestmark = postgres_only


def _body(ticket_type: TicketType, quantity: int) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": quantity}],
        "attendees": [{"name": f"Guest {n}"} for n in range(quantity)],
    }


def register(session: Session, buyer: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    data = RegistrationCreate.model_validate(_body(ticket_type, quantity))
    return service.register_free(session, buyer, ticket_type.event_id, data)


def checkout(session: Session, buyer: User, ticket_type: TicketType, quantity: int) -> OrderOut:
    data = CheckoutCreate.model_validate(_body(ticket_type, quantity))
    return service.checkout(session, buyer, ticket_type.event_id, data)


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def owner(db_session: Session, organizer: OrganizerProfile) -> User:
    return db_session.get_one(User, organizer.user_id)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    event = create_event(db_session, organizer)
    event.paid_sales_active = True  # so the pending-checkout tests can check out
    db_session.flush()
    return event


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event, quantity_total=5)


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, quantity_total=5)


@pytest.fixture
def buyer(db_session: Session) -> User:
    return create_user(db_session)


@pytest.fixture
def free_order(db_session: Session, buyer: User, free_type: TicketType) -> OrderOut:
    """A free registration for 2 tickets."""
    return register(db_session, buyer, free_type, 2)


def _stored(session: Session, order_id: uuid.UUID) -> tuple[OrderStatus, list[TicketStatus]]:
    order = repository.get_order_with_details(session, order_id, reload=True)
    statuses = sorted(t.status for item in order.items for t in item.tickets)
    return order.status, statuses


def _audit_rows(session: Session, event: Event) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.event_id == event.id)))


# --- who may cancel ---


def test_buyer_cancels_free_order(
    db_session: Session, buyer: User, event: Event, free_order: OrderOut
) -> None:
    out = service.cancel_order(db_session, buyer, free_order.id)

    assert out.status == OrderStatus.CANCELLED
    assert [t.status for t in out.tickets] == [TicketStatus.CANCELLED] * 2
    assert _stored(db_session, free_order.id) == (
        OrderStatus.CANCELLED,
        [TicketStatus.CANCELLED] * 2,
    )
    [row] = _audit_rows(db_session, event)
    assert (row.action_type, row.entity_type, row.entity_id) == (
        "order.cancelled",
        "order",
        free_order.id,
    )
    assert (row.actor_user_id, row.event_id) == (buyer.id, event.id)
    assert row.details == {"by": "buyer", "ticket_ids": sorted(str(t.id) for t in out.tickets)}


def test_event_owner_cancels_another_users_free_order(
    db_session: Session, owner: User, event: Event, free_order: OrderOut
) -> None:
    out = service.cancel_order(db_session, owner, free_order.id)

    assert out.status == OrderStatus.CANCELLED
    [row] = _audit_rows(db_session, event)
    assert row.actor_user_id == owner.id
    assert row.details["by"] == "organizer"


@pytest.mark.parametrize("who", ["another buyer", "non-organizer", "other organizer"])
def test_anyone_else_gets_404(
    db_session: Session, free_type: TicketType, free_order: OrderOut, who: str
) -> None:
    if who == "another buyer":  # has an order for the same event
        caller = create_user(db_session)
        register(db_session, caller, free_type, 1)
    elif who == "non-organizer":
        caller = create_user(db_session)
    else:
        caller = db_session.get_one(User, create_organizer(db_session).user_id)

    with pytest.raises(NotFoundError, match="^Order not found$"):
        service.cancel_order(db_session, caller, free_order.id)

    assert _stored(db_session, free_order.id) == (
        OrderStatus.FREE_CONFIRMED,
        [TicketStatus.VALID] * 2,
    )


def test_unknown_order_is_404(db_session: Session, buyer: User) -> None:
    with pytest.raises(NotFoundError, match="^Order not found$"):
        service.cancel_order(db_session, buyer, uuid.uuid4())


# --- pending checkouts ---


def test_buyer_cancels_own_pending_checkout(
    db_session: Session, buyer: User, paid_type: TicketType
) -> None:
    pending = checkout(db_session, buyer, paid_type, 3)
    assert service.get_availability(db_session, [paid_type])[paid_type.id] == 2

    out = service.cancel_order(db_session, buyer, pending.id)

    assert (out.status, out.tickets) == (OrderStatus.CANCELLED, [])
    # The hold is released at once, not after it expires.
    assert service.get_availability(db_session, [paid_type])[paid_type.id] == 5


def test_organizer_cannot_cancel_a_pending_checkout(
    db_session: Session, owner: User, buyer: User, paid_type: TicketType
) -> None:
    pending = checkout(db_session, buyer, paid_type, 1)

    with pytest.raises(BusinessRuleError, match="^Only the buyer can cancel a pending checkout$"):
        service.cancel_order(db_session, owner, pending.id)

    assert _stored(db_session, pending.id)[0] == OrderStatus.PENDING


# --- refused ---


def test_paid_order_is_refunded_not_cancelled(
    db_session: Session, owner: User, paid_type: TicketType
) -> None:
    paid = create_order_item(db_session, paid_type, 1, OrderStatus.PAID)

    with pytest.raises(BusinessRuleError, match="^Paid orders are refunded by the organizer$"):
        service.cancel_order(db_session, owner, paid.order_id)


def test_cancelling_twice_is_refused(
    db_session: Session, buyer: User, event: Event, free_order: OrderOut
) -> None:
    service.cancel_order(db_session, buyer, free_order.id)

    with pytest.raises(BusinessRuleError, match="^Order is already cancelled$"):
        service.cancel_order(db_session, buyer, free_order.id)

    assert len(_audit_rows(db_session, event)) == 1


def test_refunded_order_is_refused(db_session: Session, buyer: User, free_order: OrderOut) -> None:
    db_session.get_one(Order, free_order.id).status = OrderStatus.REFUNDED
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="^Order is already refunded$"):
        service.cancel_order(db_session, buyer, free_order.id)


@pytest.mark.parametrize("who", ["buyer", "owner"])
def test_after_the_start_is_refused(
    db_session: Session,
    request: pytest.FixtureRequest,
    event: Event,
    free_order: OrderOut,
    who: str,
) -> None:
    caller = request.getfixturevalue(who)

    with pytest.raises(BusinessRuleError, match="^Event has already started$"):
        service.cancel_order(db_session, caller, free_order.id, now=event.start_at)

    assert _stored(db_session, free_order.id)[0] == OrderStatus.FREE_CONFIRMED


def test_order_with_a_used_ticket_is_refused(
    db_session: Session, buyer: User, event: Event, free_order: OrderOut
) -> None:
    db_session.get_one(Ticket, free_order.tickets[0].id).status = TicketStatus.CHECKED_IN
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="^A ticket of this order was already used$"):
        service.cancel_order(db_session, buyer, free_order.id)

    assert _stored(db_session, free_order.id) == (
        OrderStatus.FREE_CONFIRMED,
        [TicketStatus.CHECKED_IN, TicketStatus.VALID],
    )
    assert _audit_rows(db_session, event) == []


def test_locks_event_then_order_then_tickets(
    db_session: Session, buyer: User, free_order: OrderOut, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    for name in ("lock_event", "lock_order", "lock_order_tickets"):
        real = getattr(repository, name)

        def spy(*args: object, _real=real, _name=name) -> object:
            calls.append(_name)
            return _real(*args)

        monkeypatch.setattr(repository, name, spy)

    service.cancel_order(db_session, buyer, free_order.id)

    assert calls == ["lock_event", "lock_order", "lock_order_tickets"]


# --- API ---


Headers = dict[str, str]


def _headers(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def test_api_cancel_returns_the_cancelled_order(
    api: TestClient, buyer: User, free_order: OrderOut
) -> None:
    response = api.post(f"/orders/{free_order.id}/cancel", headers=_headers(buyer))

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"]) == (str(free_order.id), "cancelled")
    assert [t["status"] for t in body["tickets"]] == ["cancelled", "cancelled"]


def test_api_requires_login(api: TestClient, free_order: OrderOut) -> None:
    assert api.post(f"/orders/{free_order.id}/cancel").status_code == 401


def test_api_stranger_gets_404(api: TestClient, db_session: Session, free_order: OrderOut) -> None:
    response = api.post(
        f"/orders/{free_order.id}/cancel", headers=_headers(create_user(db_session))
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Order not found"}


def test_api_paid_order_is_409(
    api: TestClient, db_session: Session, owner: User, paid_type: TicketType
) -> None:
    paid = create_order_item(db_session, paid_type, 1, OrderStatus.PAID)

    response = api.post(f"/orders/{paid.order_id}/cancel", headers=_headers(owner))

    assert response.status_code == 409
    assert response.json() == {"detail": "Paid orders are refunded by the organizer"}


def test_api_scanner_answers_cancelled_after_cancel(
    api: TestClient, owner: User, buyer: User, event: Event, free_order: OrderOut
) -> None:
    assert api.post(f"/orders/{free_order.id}/cancel", headers=_headers(buyer)).status_code == 200

    scan = api.post(
        "/checkin/scan",
        json={"event_id": str(event.id), "qr_token": free_order.tickets[0].qr_token},
        headers=_headers(owner),
    )

    assert scan.status_code == 200
    assert scan.json()["result"] == "cancelled"


def test_api_cancel_gives_the_tickets_back_to_sale(
    api: TestClient,
    owner: User,
    buyer: User,
    event: Event,
    free_type: TicketType,
    free_order: OrderOut,
) -> None:
    def free_type_row() -> dict:
        listed = api.get(f"/events/{event.id}/ticket-types", headers=_headers(owner)).json()
        return next(t for t in listed if t["id"] == str(free_type.id))

    assert (free_type_row()["sold"], free_type_row()["available"]) == (2, 3)

    assert api.post(f"/orders/{free_order.id}/cancel", headers=_headers(buyer)).status_code == 200

    assert (free_type_row()["sold"], free_type_row()["available"]) == (0, 5)


# --- race with the door scanner (real connections, committed rows) ---


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


@pytest.mark.slow
def test_scan_during_a_cancel_waits_and_answers_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan arriving while a cancel holds its locks must not admit the guest.

    The cancel is paused after taking its locks (inside audit.log, before its commit). The
    scan locks the ticket row, so it waits; once the cancel commits it sees `cancelled`.
    Without the ticket locks the scan would check the guest in and the cancel would then
    overwrite the used ticket with `cancelled`.
    """
    with SessionLocal() as setup:
        organizer = create_organizer(setup)
        event = create_event(setup, organizer)
        free_type = create_free_ticket_type(setup, event, quantity_total=5)
        buyer = create_user(setup)
        setup.commit()
        order = register(setup, buyer, free_type, 1)  # commits
        ids = {
            "event": event.id,
            "venue": event.venue_id,
            "organizer": organizer.id,
            "users": [organizer.user_id, buyer.id],
            "owner": organizer.user_id,
            "buyer": buyer.id,
        }
    token, order_id = order.tickets[0].qr_token, order.id

    paused, release = threading.Event(), threading.Event()
    real_log = audit_service.log

    def paused_log(*args: object, **kwargs: object) -> object:
        paused.set()
        assert release.wait(10)
        return real_log(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log", paused_log)
    results: dict[str, object] = {}

    def cancel() -> None:
        with SessionLocal() as session:
            out = service.cancel_order(session, session.get_one(User, ids["buyer"]), order_id)
            results["cancel"] = out.status

    def scan() -> None:
        with SessionLocal() as session:
            owner = session.get_one(User, ids["owner"])
            results["scan"] = checkin_service.scan(session, owner, ids["event"], token).result

    try:
        canceller = threading.Thread(target=cancel)
        canceller.start()
        assert paused.wait(10), "cancel never reached audit.log"
        scanner = threading.Thread(target=scan)
        scanner.start()
        _wait_until(lambda: not scanner.is_alive() or _lock_waiters() > 0)
        release.set()
        canceller.join(timeout=30)
        scanner.join(timeout=30)

        assert results == {"cancel": OrderStatus.CANCELLED, "scan": ScanResult.CANCELLED}
        with SessionLocal() as check:
            ticket = check.scalars(select(Ticket).where(Ticket.qr_token == token)).one()
            assert ticket.status == TicketStatus.CANCELLED
            # Nobody was admitted on the cancelled ticket.
            records = select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)
            assert check.scalars(records).all() == []
    finally:
        release.set()
        with SessionLocal() as cleanup:
            order_ids = select(Order.id).where(Order.event_id == ids["event"])
            item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
            ticket_ids = select(Ticket.id).where(Ticket.order_item_id.in_(item_ids))
            cleanup.execute(delete(CheckInRecord).where(CheckInRecord.ticket_id.in_(ticket_ids)))
            cleanup.execute(delete(AuditLog).where(AuditLog.event_id == ids["event"]))
            cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
            cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
            cleanup.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            cleanup.execute(delete(Order).where(Order.event_id == ids["event"]))
            cleanup.execute(delete(TicketType).where(TicketType.event_id == ids["event"]))
            cleanup.execute(delete(Event).where(Event.id == ids["event"]))
            cleanup.execute(delete(Venue).where(Venue.id == ids["venue"]))
            cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == ids["organizer"]))
            cleanup.execute(delete(Notification).where(Notification.user_id.in_(ids["users"])))
            cleanup.execute(delete(User).where(User.id.in_(ids["users"])))
            cleanup.commit()


def test_start_time_is_read_after_the_locks(
    db_session: Session, buyer: User, event: Event, free_order: OrderOut
) -> None:
    """now is compared with the locked event row's start_at (a moved start counts)."""
    event.start_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    with pytest.raises(BusinessRuleError, match="^Event has already started$"):
        service.cancel_order(db_session, buyer, free_order.id)
