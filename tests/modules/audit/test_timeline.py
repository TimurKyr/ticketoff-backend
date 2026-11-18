"""GET /events/{event_id}/timeline (BE-19): an event's audit log, oldest first."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_session
from app.main import app
from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.checkin import service as checkin_service
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.models import CheckInRecord, Notification
from app.modules.orders import service as orders_service
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate, RegistrationCreate
from app.modules.payments import service as payments_service
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

T = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
# (action_type, hours after T): what the timeline of `event` holds in the filter tests.
ROWS = [
    ("payment.activation_succeeded", 0),
    ("payment.order_succeeded", 1),
    ("order.cancelled", 2),
    ("order.refunded", 3),
    ("ticket.check_in_reversed", 4),
]


def add_row(session: Session, event: Event, action_type: str, at: datetime) -> AuditLog:
    """An audit row with a chosen created_at (the DB default would be the test's now())."""
    entry = audit.log(
        session,
        actor_user_id=None,
        action_type=action_type,
        entity_type=action_type.split(".")[0],
        entity_id=uuid.uuid4(),
        event_id=event.id,
    )
    entry.created_at = at
    session.flush()
    return entry


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def owner(db_session: Session, organizer: OrganizerProfile) -> User:
    return db_session.get_one(User, organizer.user_id)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    event = create_event(db_session, organizer)
    for action_type, hours in ROWS:
        add_row(db_session, event, action_type, T + hours * HOUR)
    # The same owner's other event: its rows never show up in `event`'s timeline.
    other = create_event(db_session, organizer)
    add_row(db_session, other, "order.cancelled", T + HOUR)
    return event


def actions(session: Session, event: Event, **filters: object) -> list[str]:
    return [item.action_type for item in audit.get_timeline(session, event.id, **filters).items]


def hours(*offsets: int) -> list[str]:
    return [ROWS[h][0] for h in offsets]


# --- filters (service) ---


def test_whole_timeline_oldest_first_and_only_this_event(db_session: Session, event: Event) -> None:
    page = audit.get_timeline(db_session, event.id)

    assert [i.action_type for i in page.items] == hours(0, 1, 2, 3, 4)
    assert [i.created_at for i in page.items] == [T + h * HOUR for _, h in ROWS]
    assert (page.total, page.limit, page.offset) == (5, 20, 0)


def test_from_date_is_inclusive(db_session: Session, event: Event) -> None:
    assert actions(db_session, event, from_date=T + 2 * HOUR) == hours(2, 3, 4)


def test_to_date_is_exclusive(db_session: Session, event: Event) -> None:
    assert actions(db_session, event, to_date=T + 2 * HOUR) == hours(0, 1)


def test_from_and_to_together(db_session: Session, event: Event) -> None:
    assert actions(db_session, event, from_date=T + HOUR, to_date=T + 3 * HOUR) == hours(1, 2)


@pytest.mark.parametrize(
    ("action_type", "expected"),
    [
        ("order.cancelled", hours(2)),  # exact
        ("order.", hours(2, 3)),  # prefix
        ("payment.", hours(0, 1)),
        ("order", []),  # no dot: exact match only, nothing is called just "order"
        ("order.cancel", []),  # exact, so not a prefix of "order.cancelled"
    ],
)
def test_action_type_exact_or_prefix(
    db_session: Session, event: Event, action_type: str, expected: list[str]
) -> None:
    assert actions(db_session, event, action_type=action_type) == expected


def test_prefix_treats_underscore_literally(db_session: Session, event: Event) -> None:
    add_row(db_session, event, "seat_hold.released", T + 5 * HOUR)
    add_row(db_session, event, "seatXhold.released", T + 6 * HOUR)  # "_" as a wildcard would match

    assert actions(db_session, event, action_type="seat_hold.") == ["seat_hold.released"]


def test_limit_offset_and_total(db_session: Session, event: Event) -> None:
    page = audit.get_timeline(db_session, event.id, limit=2, offset=1)

    assert [i.action_type for i in page.items] == hours(1, 2)
    assert (page.total, page.limit, page.offset) == (5, 2, 1)
    filtered = audit.get_timeline(db_session, event.id, action_type="order.", limit=1)
    assert (len(filtered.items), filtered.total) == (1, 2)  # total uses the same filters


def test_same_timestamp_rows_are_ordered_by_id(db_session: Session, organizer) -> None:
    event = create_event(db_session, organizer)
    rows = [add_row(db_session, event, "order.cancelled", T) for _ in range(5)]

    page = audit.get_timeline(db_session, event.id)

    assert [i.id for i in page.items] == sorted(r.id for r in rows)


def test_event_without_rows_is_empty(db_session: Session, organizer) -> None:
    page = audit.get_timeline(db_session, create_event(db_session, organizer).id)

    assert (page.items, page.total) == ([], 0)


# --- API ---


Headers = dict[str, str]


def _headers(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def test_api_page_shape(api: TestClient, owner: User, event: Event) -> None:
    response = api.get(
        f"/events/{event.id}/timeline",
        params={"action_type": "order.", "from_date": (T + HOUR).isoformat(), "limit": 1},
        headers=_headers(owner),
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (2, 1, 0)
    [item] = body["items"]
    assert set(item) == {
        "id",
        "created_at",
        "action_type",
        "entity_type",
        "entity_id",
        "actor_user_id",
        "details",
    }
    assert (item["action_type"], item["entity_type"]) == ("order.cancelled", "order")


def test_api_requires_login(api: TestClient, event: Event) -> None:
    assert api.get(f"/events/{event.id}/timeline").status_code == 401


def test_api_another_organizers_event_is_404(
    api: TestClient, db_session: Session, event: Event
) -> None:
    stranger = db_session.get_one(User, create_organizer(db_session).user_id)

    response = api.get(f"/events/{event.id}/timeline", headers=_headers(stranger))

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


@pytest.mark.parametrize("method", ["patch", "delete", "put", "post"])
def test_api_timeline_cannot_be_changed(
    api: TestClient, owner: User, event: Event, method: str
) -> None:
    response = api.request(method, f"/events/{event.id}/timeline", headers=_headers(owner))

    assert response.status_code == 405


@pytest.mark.parametrize(
    "params",
    [{"limit": 101}, {"offset": -1}, {"from_date": "2026-11-01T12:00:00"}],  # naive datetime
)
def test_api_invalid_query_is_422(api: TestClient, owner: User, event: Event, params: dict) -> None:
    response = api.get(f"/events/{event.id}/timeline", params=params, headers=_headers(owner))

    assert response.status_code == 422


# --- a real flow (committed rows, one transaction per action) ---


def _body(ticket_type: TicketType) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        "attendees": [{"name": "Guest"}],
    }


def test_real_actions_appear_in_order(client: TestClient) -> None:
    """Activation pay, order pay, cancel, refund and a check-in undo, each in its own
    transaction (so with its own now()), read back through the real endpoint."""
    s = SessionLocal()
    try:
        organizer = create_organizer(s)
        event = create_event(s, organizer)
        event.paid_sales_active = True
        other = create_event(s, organizer)
        paid_type = create_ticket_type(s, event, price_kzt=5000, quantity_total=5)
        free_type = create_free_ticket_type(s, event, quantity_total=5)
        buyer = create_user(s)
        owner = s.get_one(User, organizer.user_id)
        s.commit()
        ids = {"events": [event.id, other.id], "users": [owner.id, buyer.id]}
        ids["venues"] = [event.venue_id, other.venue_id]
        ids["organizer"] = organizer.id

        payments_service.pay_activation(s, event, owner)
        payments_service.pay_activation(s, other, owner)  # another event's row
        pending = orders_service.checkout(
            s, buyer, event.id, CheckoutCreate.model_validate(_body(paid_type))
        )
        paid = payments_service.pay_order(s, buyer, pending.id, PayOrderIn(simulate="success"))
        free = orders_service.register_free(
            s, buyer, event.id, RegistrationCreate.model_validate(_body(free_type))
        )
        orders_service.cancel_order(s, buyer, free.id)
        payments_service.refund_order(s, owner, pending.id, RefundIn(reason="Moved"))
        scanned = orders_service.register_free(
            s, buyer, event.id, RegistrationCreate.model_validate(_body(free_type))
        )
        ticket = scanned.tickets[0]
        checkin_service.scan(s, owner, event.id, ticket.qr_token)
        checkin_service.undo_check_in(s, owner, ticket.id)

        response = client.get(f"/events/{event.id}/timeline", headers=_headers(owner))

        assert response.status_code == 200
        items = response.json()["items"]
        assert [(i["action_type"], i["entity_type"]) for i in items] == [
            ("payment.activation_succeeded", "payment"),
            ("payment.order_succeeded", "payment"),
            ("order.cancelled", "order"),
            ("order.refunded", "order"),
            ("ticket.check_in_reversed", "ticket"),
        ]
        assert [i["actor_user_id"] for i in items] == [
            str(owner.id),
            str(buyer.id),
            str(buyer.id),
            str(owner.id),
            str(owner.id),
        ]
        assert items[1]["entity_id"] == str(paid.payment.id)
        assert items[2]["entity_id"] == str(free.id)
        assert items[3]["entity_id"] == str(pending.id)
        assert items[4]["entity_id"] == str(ticket.id)
        created = [i["created_at"] for i in items]
        assert created == sorted(created) and len(set(created)) == 5  # one transaction each
        assert response.json()["total"] == 5
    finally:
        s.rollback()
        order_ids = select(Order.id).where(Order.event_id.in_(ids["events"]))
        item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        ticket_ids = select(Ticket.id).where(Ticket.order_item_id.in_(item_ids))
        payment_ids = select(Payment.id).where(
            or_(Payment.order_id.in_(order_ids), Payment.event_id.in_(ids["events"]))
        )
        s.execute(delete(CheckInRecord).where(CheckInRecord.ticket_id.in_(ticket_ids)))
        s.execute(delete(Notification).where(Notification.user_id.in_(ids["users"])))
        s.execute(delete(AuditLog).where(AuditLog.event_id.in_(ids["events"])))
        s.execute(delete(Refund).where(Refund.payment_id.in_(payment_ids)))
        s.execute(delete(Payment).where(Payment.id.in_(payment_ids)))
        s.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        s.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
        s.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        s.execute(delete(Order).where(Order.event_id.in_(ids["events"])))
        s.execute(delete(TicketType).where(TicketType.event_id.in_(ids["events"])))
        s.execute(delete(Event).where(Event.id.in_(ids["events"])))
        s.execute(delete(Venue).where(Venue.id.in_(ids["venues"])))
        s.execute(delete(OrganizerProfile).where(OrganizerProfile.id == ids["organizer"]))
        s.execute(delete(User).where(User.id.in_(ids["users"])))
        s.commit()
        s.close()
