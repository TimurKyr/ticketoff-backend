"""Organizer inventory summary (BE-12 PR B): GET /events/{event_id}/inventory."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile
from app.modules.orders import repository, service
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, OrderItem, Ticket, TicketType
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_order_item,
    create_organizer,
    create_ticket_type,
)
from tests.markers import postgres_only

# Timezone-aware created_at comparisons and FILTER aggregates behave as in production.
pytestmark = postgres_only

Headers = dict[str, str]


def add_tickets(session: Session, item: OrderItem, *statuses: TicketStatus) -> None:
    """One ticket (with its attendee) per status, on the item's order."""
    for status in statuses:
        attendee = Attendee(order_id=item.order_id, order_item_id=item.id, name="Guest")
        session.add(attendee)
        session.flush()
        session.add(
            Ticket(
                order_item_id=item.id,
                attendee_id=attendee.id,
                qr_token=uuid.uuid4().hex,
                status=status,
            )
        )
    session.flush()


@dataclass
class Stocked:
    event: Event
    free: TicketType
    standard: TicketType
    vip: TicketType


def stock(session: Session, event: Event, now: datetime) -> Stocked:
    """Every kind of order and ticket status, with distinct quantities so a wrongly counted
    order shows up as a wrong number."""
    free = create_free_ticket_type(session, event, name="Free", quantity_total=50)
    standard = create_ticket_type(session, event, name="Standard", quantity_total=100)
    vip = create_ticket_type(
        session, event, name="VIP", price_kzt=20000, quantity_total=10, is_hidden=True
    )

    confirmed_free = create_order_item(session, free, 2, OrderStatus.FREE_CONFIRMED)
    add_tickets(session, confirmed_free, TicketStatus.VALID, TicketStatus.CHECKED_IN)

    paid = create_order_item(session, standard, 3, OrderStatus.PAID)
    add_tickets(session, paid, TicketStatus.VALID, TicketStatus.CHECKED_IN, TicketStatus.CHECKED_IN)
    # Ticket statuses changed while the order stays paid: still sold (order-based).
    paid_with_returns = create_order_item(session, standard, 2, OrderStatus.PAID)
    add_tickets(session, paid_with_returns, TicketStatus.REFUNDED, TicketStatus.CANCELLED)

    create_order_item(session, standard, 4, OrderStatus.PENDING)  # fresh: reserved
    expired = now - repository.checkout_hold() - timedelta(minutes=1)
    create_order_item(session, standard, 5, OrderStatus.PENDING, created_at=expired)
    create_order_item(session, standard, 6, OrderStatus.CANCELLED)
    create_order_item(session, standard, 7, OrderStatus.REFUNDED)
    return Stocked(event, free, standard, vip)


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    return create_event(db_session, organizer)


# --- numbers ---


def test_inventory_counts_every_order_and_ticket_status(db_session: Session, event: Event) -> None:
    now = datetime.now(UTC)
    s = stock(db_session, event, now)

    out = service.get_inventory(db_session, event, now=now)

    rows = [t.model_dump() for t in out.ticket_types]
    assert rows == [
        {
            "ticket_type_id": s.free.id,
            "name": "Free",
            "quantity_total": 50,
            "sold": 2,
            "reserved": 0,
            "available": 48,
            "checked_in": 1,
            "refunded": 0,
            "cancelled": 0,
        },
        {
            "ticket_type_id": s.standard.id,
            "name": "Standard",
            "quantity_total": 100,
            "sold": 5,  # 3 + 2 paid; pending 4 is reserved; expired 5, cancelled 6, refunded 7 not
            "reserved": 4,
            "available": 91,
            "checked_in": 2,
            "refunded": 1,
            "cancelled": 1,
        },
        {  # hidden and without orders: still listed, with zeros
            "ticket_type_id": s.vip.id,
            "name": "VIP",
            "quantity_total": 10,
            "sold": 0,
            "reserved": 0,
            "available": 10,
            "checked_in": 0,
            "refunded": 0,
            "cancelled": 0,
        },
    ]
    totals = out.model_dump(exclude={"ticket_types"})
    assert totals == {
        "event_id": event.id,
        "capacity": None,
        "sold": 7,
        "reserved": 4,
        "available": 149,
        "checked_in": 3,
        "refunded": 1,
        "cancelled": 1,
    }


def test_reservation_expires_with_the_hold(db_session: Session, event: Event) -> None:
    standard = create_ticket_type(db_session, event, quantity_total=10)
    item = create_order_item(db_session, standard, 4, OrderStatus.PENDING)
    created_at = db_session.get(OrderItem, item.id).order.created_at
    hold = repository.checkout_hold()

    inside = service.get_inventory(db_session, event, now=created_at + hold - timedelta(seconds=1))
    after = service.get_inventory(db_session, event, now=created_at + hold)

    assert (inside.reserved, inside.available) == (4, 6)
    assert (after.reserved, after.available) == (0, 10)


def test_event_without_ticket_types_is_empty(db_session: Session, event: Event) -> None:
    out = service.get_inventory(db_session, event)

    assert out.ticket_types == []
    assert (out.sold, out.reserved, out.available) == (0, 0, 0)


@pytest.mark.parametrize(
    ("capacity", "available"),
    [
        (None, 11),  # no capacity: the sum of the types (5 + 6)
        (100, 11),  # capacity not binding
        (10, 5),  # capped: 10 - (3 sold + 2 reserved)
        (4, 0),  # already over capacity (update_event can lower it): never below 0
    ],
)
def test_event_available_is_capped_by_capacity(
    db_session: Session, event: Event, capacity: int | None, available: int
) -> None:
    a = create_ticket_type(db_session, event, name="A", quantity_total=8)
    b = create_ticket_type(db_session, event, name="B", quantity_total=8)
    create_order_item(db_session, a, 3, OrderStatus.PAID)
    create_order_item(db_session, b, 2, OrderStatus.PENDING)
    event.capacity = capacity
    db_session.flush()

    out = service.get_inventory(db_session, event)

    assert [t.available for t in out.ticket_types] == [5, 6]  # per type: never capped
    assert (out.capacity, out.sold, out.reserved, out.available) == (capacity, 3, 2, available)


# --- API ---


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def login(api: TestClient) -> Callable[[], Headers]:
    def make() -> Headers:
        email, password = f"user-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"
        assert (
            api.post("/auth/register", json={"email": email, "password": password}).status_code
            == 201
        )
        token = api.post("/auth/login", data={"username": email, "password": password}).json()[
            "access_token"
        ]
        return {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def make_organizer(
    api: TestClient, login: Callable[[], Headers], db_session: Session
) -> Callable[[], tuple[Headers, OrganizerProfile]]:
    def make() -> tuple[Headers, OrganizerProfile]:
        headers = login()
        response = api.post("/organizer/profile", json={"display_name": "Org"}, headers=headers)
        assert response.status_code == 201, response.text
        return headers, db_session.get_one(OrganizerProfile, response.json()["id"])

    return make


def test_inventory_agrees_with_ticket_type_list(
    api: TestClient,
    db_session: Session,
    make_organizer: Callable[[], tuple[Headers, OrganizerProfile]],
) -> None:
    """sold + reserved == TicketTypeOut.sold and available == TicketTypeOut.available."""
    headers, profile = make_organizer()
    owned = create_event(db_session, profile)
    stock(db_session, owned, datetime.now(UTC))

    inventory = api.get(f"/events/{owned.id}/inventory", headers=headers)
    ticket_types = api.get(f"/events/{owned.id}/ticket-types", headers=headers)

    assert inventory.status_code == ticket_types.status_code == 200
    listed = {t["id"]: t for t in ticket_types.json()}
    rows = inventory.json()["ticket_types"]
    assert {r["ticket_type_id"] for r in rows} == set(listed)
    for row in rows:
        listed_type = listed[row["ticket_type_id"]]
        assert row["sold"] + row["reserved"] == listed_type["sold"]
        assert row["available"] == listed_type["available"]
        assert row["quantity_total"] == listed_type["quantity_total"]


def test_requires_login(api: TestClient, event: Event) -> None:
    assert api.get(f"/events/{event.id}/inventory").status_code == 401


def test_requires_organizer(api: TestClient, login: Callable[[], Headers], event: Event) -> None:
    response = api.get(f"/events/{event.id}/inventory", headers=login())

    assert response.status_code == 403
    assert response.json() == {"detail": "Organizer account required"}


def test_another_organizers_event_is_404(
    api: TestClient,
    event: Event,
    make_organizer: Callable[[], tuple[Headers, OrganizerProfile]],
) -> None:
    headers, _ = make_organizer()

    response = api.get(f"/events/{event.id}/inventory", headers=headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_unknown_event_is_404(
    api: TestClient, make_organizer: Callable[[], tuple[Headers, OrganizerProfile]]
) -> None:
    headers, _ = make_organizer()

    assert api.get(f"/events/{uuid.uuid4()}/inventory", headers=headers).status_code == 404
