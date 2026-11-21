"""EventOut.price_from / availability and GET /events?city= (BE-24)."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from app.modules.events.models import Event, Venue
from app.modules.events.schemas import Availability, availability_of
from app.modules.identity.models import OrganizerProfile
from app.modules.orders.enums import OrderStatus
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_order_item,
    create_organizer,
    create_ticket_type,
)
from tests.markers import postgres_only

pytestmark = postgres_only


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:10]


def _event(db: Session, organizer: OrganizerProfile, title: str, city: str | None = None) -> Event:
    event = create_event(db, organizer)
    event.title = title
    if city is not None:
        db.get(Venue, event.venue_id).city = city
    db.flush()
    return event


def _listed(api: TestClient, **params: object) -> dict[str, dict]:
    response = api.get("/events", params={"limit": 100, **params})
    assert response.status_code == 200, response.text
    return {item["title"]: item for item in response.json()["items"]}


def test_price_from_follows_the_public_ticket_type_rules(
    db_session: Session, session_api: TestClient, organizer: OrganizerProfile, tag: str
) -> None:
    now = datetime.now(UTC)
    paid = _event(db_session, organizer, f"{tag} paid")
    create_ticket_type(db_session, paid, name="A", price_kzt=8000)
    create_ticket_type(db_session, paid, name="B", price_kzt=3000)
    # Cheaper, but not on sale: hidden, not started yet, already ended.
    create_ticket_type(db_session, paid, name="Hidden", price_kzt=500, is_hidden=True)
    create_ticket_type(
        db_session, paid, name="Later", price_kzt=600, sales_start_at=now + timedelta(days=1)
    )
    create_ticket_type(
        db_session,
        paid,
        name="Ended",
        price_kzt=700,
        sales_start_at=now - timedelta(days=2),
        sales_end_at=now - timedelta(days=1),
    )

    free = _event(db_session, organizer, f"{tag} free")
    create_ticket_type(db_session, free, name="Paid", price_kzt=5000)
    create_free_ticket_type(db_session, free, name="Free")

    _event(db_session, organizer, f"{tag} no types")
    hidden_only = _event(db_session, organizer, f"{tag} hidden only")
    create_ticket_type(db_session, hidden_only, price_kzt=100, is_hidden=True)

    listed = _listed(session_api, q=tag)
    assert listed[f"{tag} paid"]["price_from"] == 3000
    assert listed[f"{tag} free"]["price_from"] == 0
    assert listed[f"{tag} no types"]["price_from"] is None
    assert listed[f"{tag} no types"]["availability"] is None
    assert listed[f"{tag} hidden only"]["price_from"] is None

    # The event page agrees with the list, and with the public ticket types.
    page = session_api.get(f"/events/{paid.id}").json()
    assert page["price_from"] == 3000
    public = session_api.get(f"/events/{paid.id}/ticket-types/public").json()
    assert min(t["price_kzt"] for t in public) == page["price_from"]


def test_sold_out_type_still_counts_for_price(
    db_session: Session, session_api: TestClient, organizer: OrganizerProfile, tag: str
) -> None:
    # The public ticket type list shows sold-out types (available 0); price_from does too.
    event = _event(db_session, organizer, f"{tag} sold out")
    cheap = create_ticket_type(db_session, event, name="Cheap", price_kzt=1000, quantity_total=5)
    create_order_item(db_session, cheap, 5)

    item = _listed(session_api, q=tag)[f"{tag} sold out"]
    assert item["price_from"] == 1000
    assert item["availability"] == "sold_out"


def test_availability(
    db_session: Session, session_api: TestClient, organizer: OrganizerProfile, tag: str
) -> None:
    plenty = _event(db_session, organizer, f"{tag} plenty")
    create_ticket_type(db_session, plenty, quantity_total=100)

    few = _event(db_session, organizer, f"{tag} few")
    few_type = create_ticket_type(db_session, few, quantity_total=100)
    create_order_item(db_session, few_type, 91)
    # Cancelled and expired checkouts give their tickets back.
    create_order_item(db_session, few_type, 5, status=OrderStatus.CANCELLED)
    create_order_item(
        db_session,
        few_type,
        5,
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )

    sold_out = _event(db_session, organizer, f"{tag} sold")
    sold_type = create_ticket_type(db_session, sold_out, quantity_total=3)
    create_order_item(db_session, sold_type, 3)
    hidden = create_ticket_type(db_session, sold_out, name="Hidden", is_hidden=True)
    assert hidden.quantity_total == 100  # left over, but not on sale

    listed = _listed(session_api, q=tag)
    assert listed[f"{tag} plenty"]["availability"] == "available"
    assert listed[f"{tag} few"]["availability"] == "few_left"
    assert listed[f"{tag} sold"]["availability"] == "sold_out"


@pytest.mark.parametrize(
    ("available", "total", "expected"),
    [
        (0, 10, Availability.SOLD_OUT),
        (10, 1000, Availability.FEW_LEFT),
        (11, 100, Availability.AVAILABLE),
        (100, 1000, Availability.FEW_LEFT),  # 10%
        (101, 1000, Availability.AVAILABLE),
    ],
)
def test_availability_thresholds(available: int, total: int, expected: Availability) -> None:
    assert availability_of(available, total) == expected


def test_city_filter(
    db_session: Session, session_api: TestClient, organizer: OrganizerProfile, tag: str
) -> None:
    for title, city in (("a", "Алматы"), ("b", "Астана"), ("c", None)):
        event = _event(db_session, organizer, f"{tag} {title}", city=city)
        create_ticket_type(db_session, event)

    assert set(_listed(session_api, q=tag, city="Алматы")) == {f"{tag} a"}
    assert set(_listed(session_api, q=tag, city="Астана")) == {f"{tag} b"}
    assert set(_listed(session_api, q=tag, city="алматы")) == set()  # exact match
    assert len(_listed(session_api, q=tag)) == 3
    response = session_api.get("/events", params={"q": tag, "city": "Алматы"})
    assert response.json()["total"] == 1


@contextmanager
def _count_queries(db: Session) -> Iterator[list[str]]:
    statements: list[str] = []
    connection = db.connection()

    def before(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    sa_event.listen(connection, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        sa_event.remove(connection, "before_cursor_execute", before)


def test_list_has_no_n_plus_one(
    db_session: Session, session_api: TestClient, organizer: OrganizerProfile, tag: str
) -> None:
    def add_events(prefix: str, count: int) -> None:
        for n in range(count):
            event = _event(db_session, organizer, f"{tag} {prefix} {n}")
            create_free_ticket_type(db_session, event, name="Free")
            paid = create_ticket_type(db_session, event, name="Paid")
            create_order_item(db_session, paid, 2)

    add_events("few", 2)
    with _count_queries(db_session) as few:
        assert len(_listed(session_api, q=f"{tag} few")) == 2

    add_events("many", 8)
    with _count_queries(db_session) as many:
        listed = _listed(session_api, q=f"{tag} many")
    assert len(listed) == 8
    assert all(item["price_from"] == 0 for item in listed.values())
    assert len(many) == len(few)


def test_organizer_list_has_the_fields_too(
    db_session: Session, session_api: TestClient, organizer: OrganizerProfile
) -> None:
    from app.modules.identity.models import User
    from app.modules.identity.security import create_access_token

    event = _event(db_session, organizer, "mine")
    create_ticket_type(db_session, event, price_kzt=2500)
    user = db_session.get(User, organizer.user_id)
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}

    [item] = session_api.get("/organizer/events", headers=headers).json()["items"]
    assert item["price_from"] == 2500
    mine = session_api.get(f"/organizer/events/{event.id}", headers=headers).json()
    assert mine["availability"] == "available"
