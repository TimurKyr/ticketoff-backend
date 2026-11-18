"""POST /orders/{order_id}/refund (BE-19): full simulated refund of a paid order."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.db import SessionLocal, get_session
from app.main import app
from app.modules.audit.models import AuditLog
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.enums import NotificationChannel, NotificationType
from app.modules.ops.models import CheckInRecord, Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate, OrderOut, RegistrationCreate
from app.modules.payments import service
from app.modules.payments.enums import RefundStatus
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

AMOUNT = 10_000  # 2 tickets x 5 000 KZT


def _body(ticket_type: TicketType, quantity: int) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": quantity}],
        "attendees": [{"name": f"Guest {n}"} for n in range(quantity)],
    }


def checkout(session: Session, buyer: User, ticket_type: TicketType) -> OrderOut:
    data = CheckoutCreate.model_validate(_body(ticket_type, 2))
    return orders_service.checkout(session, buyer, ticket_type.event_id, data)


def buy(session: Session, buyer: User, ticket_type: TicketType) -> OrderOut:
    """A paid order through the real flow: checkout, then a successful simulated pay."""
    pending = checkout(session, buyer, ticket_type)
    return service.pay_order(session, buyer, pending.id, PayOrderIn(simulate="success")).order


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
    return create_ticket_type(db_session, event, price_kzt=5000, quantity_total=5)


@pytest.fixture
def buyer(db_session: Session) -> User:
    return create_user(db_session)


@pytest.fixture
def paid(db_session: Session, buyer: User, paid_type: TicketType) -> OrderOut:
    return buy(db_session, buyer, paid_type)


def _refunds(session: Session, order_id: uuid.UUID) -> list[Refund]:
    stmt = select(Refund).join(Payment, Payment.id == Refund.payment_id)
    return list(session.scalars(stmt.where(Payment.order_id == order_id)))


def _audit(session: Session, event: Event, action_type: str) -> list[AuditLog]:
    stmt = select(AuditLog).where(
        AuditLog.event_id == event.id, AuditLog.action_type == action_type
    )
    return list(session.scalars(stmt))


def _refund_notifications(session: Session, buyer: User) -> list[Notification]:
    stmt = select(Notification).where(
        Notification.user_id == buyer.id, Notification.type == NotificationType.REFUND_PROCESSED
    )
    return list(session.scalars(stmt))


def _stored(session: Session, order_id: uuid.UUID) -> tuple[OrderStatus, list[TicketStatus]]:
    order = orders_repository.get_order_with_details(session, order_id, reload=True)
    return order.status, sorted(t.status for item in order.items for t in item.tickets)


# --- the refund ---


def test_owner_refunds_paid_order(
    db_session: Session, owner: User, buyer: User, event: Event, paid: OrderOut
) -> None:
    # One guest was already admitted: a full refund covers used tickets too.
    db_session.get_one(Ticket, paid.tickets[0].id).status = TicketStatus.CHECKED_IN
    db_session.flush()

    result = service.refund_order(db_session, owner, paid.id, RefundIn(reason="Venue closed"))

    [refund] = _refunds(db_session, paid.id)
    payment = db_session.get_one(Payment, refund.payment_id)
    assert (refund.amount_kzt, refund.status, refund.reason) == (
        AMOUNT,
        RefundStatus.SUCCEEDED,
        "Venue closed",
    )
    assert result.refund.model_dump() == {
        "id": refund.id,
        "amount_kzt": AMOUNT,
        "status": RefundStatus.SUCCEEDED,
        "reason": "Venue closed",
        "is_simulated": True,
    }
    assert result.order.status == OrderStatus.REFUNDED
    assert [t.status for t in result.order.tickets] == [TicketStatus.REFUNDED] * 2
    assert _stored(db_session, paid.id) == (OrderStatus.REFUNDED, [TicketStatus.REFUNDED] * 2)

    [notification] = _refund_notifications(db_session, buyer)
    assert notification.channel == NotificationChannel.IN_APP
    assert notification.payload == {
        "order_id": str(paid.id),
        "refund_id": str(refund.id),
        "amount_kzt": AMOUNT,
    }

    [row] = _audit(db_session, event, "order.refunded")
    assert (row.actor_user_id, row.entity_type, row.entity_id) == (owner.id, "order", paid.id)
    assert row.details == {
        "refund_id": str(refund.id),
        "payment_id": str(payment.id),
        "amount_kzt": AMOUNT,
        "reason": "Venue closed",
        "is_simulated": True,
    }


def test_reason_is_optional(db_session: Session, owner: User, paid: OrderOut) -> None:
    result = service.refund_order(db_session, owner, paid.id, RefundIn())

    assert result.refund.reason is None


def test_refunding_twice_is_refused(
    db_session: Session, owner: User, buyer: User, event: Event, paid: OrderOut
) -> None:
    service.refund_order(db_session, owner, paid.id, RefundIn())

    with pytest.raises(BusinessRuleError, match="^Order is already refunded$"):
        service.refund_order(db_session, owner, paid.id, RefundIn())

    assert len(_refunds(db_session, paid.id)) == 1
    assert len(_audit(db_session, event, "order.refunded")) == 1
    assert len(_refund_notifications(db_session, buyer)) == 1


def test_refund_after_the_event_started_is_allowed(
    db_session: Session, owner: User, event: Event, paid: OrderOut
) -> None:
    event.start_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.flush()

    result = service.refund_order(db_session, owner, paid.id, RefundIn())

    assert result.order.status == OrderStatus.REFUNDED


# --- refused ---


def test_free_order_is_refused(db_session: Session, owner: User, buyer: User, event: Event) -> None:
    free_type = create_free_ticket_type(db_session, event)
    data = RegistrationCreate.model_validate(_body(free_type, 1))
    free = orders_service.register_free(db_session, buyer, event.id, data)

    with pytest.raises(BusinessRuleError, match="^Only paid orders can be refunded$"):
        service.refund_order(db_session, owner, free.id, RefundIn())


def test_pending_checkout_is_refused(
    db_session: Session, owner: User, buyer: User, paid_type: TicketType
) -> None:
    pending = checkout(db_session, buyer, paid_type)

    with pytest.raises(BusinessRuleError, match="^Only paid orders can be refunded$"):
        service.refund_order(db_session, owner, pending.id, RefundIn())


def test_cancelled_order_is_refused(
    db_session: Session, owner: User, buyer: User, paid_type: TicketType
) -> None:
    pending = checkout(db_session, buyer, paid_type)
    orders_service.cancel_order(db_session, buyer, pending.id)

    with pytest.raises(BusinessRuleError, match="^Order is already cancelled$"):
        service.refund_order(db_session, owner, pending.id, RefundIn())


@pytest.mark.parametrize("who", ["buyer", "another buyer", "non-organizer", "other organizer"])
def test_anyone_but_the_event_owner_gets_404(
    db_session: Session,
    request: pytest.FixtureRequest,
    paid_type: TicketType,
    paid: OrderOut,
    who: str,
) -> None:
    if who == "buyer":
        caller = request.getfixturevalue("buyer")
    elif who == "another buyer":  # has its own paid order for the same event
        caller = create_user(db_session)
        buy(db_session, caller, paid_type)
    elif who == "non-organizer":
        caller = create_user(db_session)
    else:
        caller = db_session.get_one(User, create_organizer(db_session).user_id)

    with pytest.raises(NotFoundError, match="^Order not found$"):
        service.refund_order(db_session, caller, paid.id, RefundIn())

    assert _refunds(db_session, paid.id) == []
    assert _stored(db_session, paid.id)[0] == OrderStatus.PAID


def test_unknown_order_is_404(db_session: Session, owner: User) -> None:
    with pytest.raises(NotFoundError, match="^Order not found$"):
        service.refund_order(db_session, owner, uuid.uuid4(), RefundIn())


# --- effects elsewhere ---


def test_refund_gives_the_tickets_back_to_sale(
    db_session: Session, owner: User, paid_type: TicketType, paid: OrderOut
) -> None:
    assert orders_service.get_availability(db_session, [paid_type])[paid_type.id] == 3

    service.refund_order(db_session, owner, paid.id, RefundIn())

    assert orders_service.get_availability(db_session, [paid_type])[paid_type.id] == 5


def test_refund_shows_in_the_payments_summary(
    db_session: Session, owner: User, event: Event, paid: OrderOut
) -> None:
    before = service.get_summary(db_session, event)
    assert (before.gross_kzt, before.fees_kzt, before.refunds_kzt) == (AMOUNT, 300, 0)
    assert before.estimated_payout_kzt == 9_700

    service.refund_order(db_session, owner, paid.id, RefundIn())

    after = service.get_summary(db_session, event)
    assert (after.gross_kzt, after.fees_kzt, after.refunds_kzt) == (AMOUNT, 300, AMOUNT)
    assert after.estimated_payout_kzt == before.estimated_payout_kzt - AMOUNT  # -300


def test_locks_event_then_order_then_tickets(
    db_session: Session, owner: User, paid: OrderOut, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    for name in ("lock_event", "lock_order", "lock_order_tickets"):
        real = getattr(orders_repository, name)

        def spy(*args: object, _real=real, _name=name) -> object:
            calls.append(_name)
            return _real(*args)

        monkeypatch.setattr(orders_repository, name, spy)

    service.refund_order(db_session, owner, paid.id, RefundIn())

    assert calls == ["lock_event", "lock_order", "lock_order_tickets"]


# --- API ---


Headers = dict[str, str]


def _headers(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def refund(api: TestClient, user: User, order_id: uuid.UUID, body: dict | None = None):
    return api.post(f"/orders/{order_id}/refund", json=body or {}, headers=_headers(user))


def test_api_refund_response(api: TestClient, owner: User, paid: OrderOut) -> None:
    response = refund(api, owner, paid.id, {"reason": "Rain"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"refund", "order"}
    assert body["refund"] | {"id": None} == {
        "id": None,
        "amount_kzt": AMOUNT,
        "status": "succeeded",
        "reason": "Rain",
        "is_simulated": True,
    }
    assert (body["order"]["id"], body["order"]["status"]) == (str(paid.id), "refunded")
    assert [t["status"] for t in body["order"]["tickets"]] == ["refunded", "refunded"]


def test_api_requires_login(api: TestClient, paid: OrderOut) -> None:
    assert api.post(f"/orders/{paid.id}/refund", json={}).status_code == 401


def test_api_buyer_gets_404(api: TestClient, buyer: User, paid: OrderOut) -> None:
    response = refund(api, buyer, paid.id)

    assert response.status_code == 404
    assert response.json() == {"detail": "Order not found"}


def test_api_too_long_reason_is_422(api: TestClient, owner: User, paid: OrderOut) -> None:
    assert refund(api, owner, paid.id, {"reason": "x" * 500}).status_code == 200
    response = refund(api, owner, paid.id, {"reason": "x" * 501})  # validated before the 409

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "reason"]


def test_api_twice_is_409(api: TestClient, owner: User, paid: OrderOut) -> None:
    assert refund(api, owner, paid.id).status_code == 200

    response = refund(api, owner, paid.id)

    assert response.status_code == 409
    assert response.json() == {"detail": "Order is already refunded"}


def test_api_scanner_answers_refunded(
    api: TestClient, owner: User, event: Event, paid: OrderOut
) -> None:
    assert refund(api, owner, paid.id).status_code == 200

    scan = api.post(
        "/checkin/scan",
        json={"event_id": str(event.id), "qr_token": paid.tickets[0].qr_token},
        headers=_headers(owner),
    )

    assert scan.status_code == 200
    assert scan.json()["result"] == "refunded"


def test_api_ticket_types_and_summary_after_refund(
    api: TestClient, owner: User, event: Event, paid_type: TicketType, paid: OrderOut
) -> None:
    def paid_type_row() -> dict:
        listed = api.get(f"/events/{event.id}/ticket-types", headers=_headers(owner)).json()
        return next(t for t in listed if t["id"] == str(paid_type.id))

    def summary() -> dict:
        return api.get(f"/events/{event.id}/payments/summary", headers=_headers(owner)).json()

    assert (paid_type_row()["sold"], paid_type_row()["available"]) == (2, 3)
    assert summary()["refunds_kzt"] == 0

    assert refund(api, owner, paid.id).status_code == 200

    assert (paid_type_row()["sold"], paid_type_row()["available"]) == (0, 5)
    assert (summary()["refunds_kzt"], summary()["estimated_payout_kzt"]) == (AMOUNT, -300)


# --- race (real connections, committed rows) ---


@pytest.mark.slow
def test_concurrent_refunds_create_one_refund() -> None:
    with SessionLocal() as setup:
        organizer = create_organizer(setup)
        event = create_event(setup, organizer)
        event.paid_sales_active = True
        paid_type = create_ticket_type(setup, event, price_kzt=5000, quantity_total=5)
        buyer = create_user(setup)
        setup.commit()
        order_id = buy(setup, buyer, paid_type).id  # commits
        ids = {
            "event": event.id,
            "venue": event.venue_id,
            "organizer": organizer.id,
            "owner": organizer.user_id,
            "users": [organizer.user_id, buyer.id],
        }

    barrier = threading.Barrier(2)
    results: list[str] = []

    def run() -> None:
        with SessionLocal() as session:
            owner = session.get_one(User, ids["owner"])
            barrier.wait()
            try:
                service.refund_order(session, owner, order_id, RefundIn())
                results.append("refunded")
            except BusinessRuleError as exc:
                results.append(str(exc))

    try:
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results) == ["Order is already refunded", "refunded"]
        with SessionLocal() as check:
            payment_ids = select(Payment.id).where(Payment.order_id == order_id)
            refunds = select(func.count(Refund.id)).where(Refund.payment_id.in_(payment_ids))
            audits = select(func.count(AuditLog.id)).where(
                AuditLog.event_id == ids["event"], AuditLog.action_type == "order.refunded"
            )
            assert (check.scalar(refunds), check.scalar(audits)) == (1, 1)
    finally:
        with SessionLocal() as cleanup:
            order_ids = select(Order.id).where(Order.event_id == ids["event"])
            item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
            ticket_ids = select(Ticket.id).where(Ticket.order_item_id.in_(item_ids))
            payment_ids = select(Payment.id).where(Payment.order_id.in_(order_ids))
            cleanup.execute(delete(CheckInRecord).where(CheckInRecord.ticket_id.in_(ticket_ids)))
            cleanup.execute(delete(Notification).where(Notification.user_id.in_(ids["users"])))
            cleanup.execute(delete(AuditLog).where(AuditLog.event_id == ids["event"]))
            cleanup.execute(delete(Refund).where(Refund.payment_id.in_(payment_ids)))
            cleanup.execute(delete(Payment).where(Payment.order_id.in_(order_ids)))
            cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
            cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
            cleanup.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            cleanup.execute(delete(Order).where(Order.event_id == ids["event"]))
            cleanup.execute(delete(TicketType).where(TicketType.event_id == ids["event"]))
            cleanup.execute(delete(Event).where(Event.id == ids["event"]))
            cleanup.execute(delete(Venue).where(Venue.id == ids["venue"]))
            cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == ids["organizer"]))
            cleanup.execute(delete(User).where(User.id.in_(ids["users"])))
            cleanup.commit()
