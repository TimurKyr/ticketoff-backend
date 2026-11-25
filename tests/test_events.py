"""Events API (BE-11).

Tests share one database, so every test makes its own users and tags its events with a unique
string, and list assertions are narrowed to that tag instead of counting all rows.
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token, hash_password
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, OrderItem

Headers = dict[str, str]
UserFactory = Callable[..., tuple[User, Headers]]


def _iso(delta_days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=delta_days)).isoformat()


@pytest.fixture
def db() -> Iterator[Session]:
    """A committing session on the real database (the API under test uses its own sessions)."""
    with SessionLocal() as session:
        yield session


@pytest.fixture
def make_user(db: Session) -> UserFactory:
    """Create a user (optionally with an organizer profile) and a Bearer header for it."""

    def _make(organizer: bool = False) -> tuple[User, Headers]:
        user = User(
            email=f"{uuid.uuid4().hex}@example.com",
            password_hash=hash_password("x" * 12),
            email_verified=True,  # publishing an event needs a confirmed email
        )
        db.add(user)
        db.flush()
        if organizer:
            db.add(OrganizerProfile(user_id=user.id, display_name="Organizer"))
        db.commit()
        db.refresh(user)
        return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}

    return _make


@pytest.fixture
def venue(db: Session) -> Venue:
    venue = Venue(name=f"Arena {uuid.uuid4().hex[:6]}", address="Abay 1", city="Almaty")
    db.add(venue)
    db.commit()
    return venue


@pytest.fixture
def org(make_user: UserFactory) -> Headers:
    return make_user(organizer=True)[1]


@pytest.fixture
def other_org(make_user: UserFactory) -> Headers:
    return make_user(organizer=True)[1]


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:10]


def _payload(venue: Venue, **overrides: object) -> dict:
    body = {
        "title": "Spring Concert",
        "description": "Live music",
        "category": "music",
        "venue_id": str(venue.id),
        "start_at": _iso(10),
        "end_at": _iso(10.1),
        "capacity": 100,
    }
    body.update(overrides)
    return body


def _create(client: TestClient, headers: Headers, venue: Venue, **overrides: object) -> dict:
    response = client.post("/events", json=_payload(venue, **overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _add_type(client: TestClient, headers: Headers, event_id: str, **overrides: object) -> dict:
    body = {"name": "Standard", "quantity_total": 10, **overrides}
    response = client.post(f"/events/{event_id}/ticket-types", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _publish(client: TestClient, headers: Headers, event_id: str) -> dict:
    """Publish with one visible ticket type (publish requires it)."""
    _add_type(client, headers, event_id)
    response = client.post(f"/events/{event_id}/publish", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _titles(client: TestClient, tag: str, **params: object) -> list[str]:
    """Public event titles matching this test's tag."""
    response = client.get("/events", params={"q": tag, **params})
    assert response.status_code == 200, response.text
    return [e["title"] for e in response.json()["items"]]


# --- venues -----------------------------------------------------------------------------


def test_venues_list_is_public(client: TestClient, venue: Venue) -> None:
    response = client.get("/venues")

    assert response.status_code == 200
    assert venue.name in [v["name"] for v in response.json()]


# --- create -----------------------------------------------------------------------------


def test_create_event_makes_a_draft(
    client: TestClient, make_user: UserFactory, venue: Venue
) -> None:
    user, headers = make_user(organizer=True)

    body = _create(client, headers, venue)

    assert body["status"] == "draft"
    assert body["visibility"] == "public"
    assert body["paid_sales_active"] is False
    assert body["organizer_id"] == str(user.organizer_profile.id)


def test_create_requires_login(client: TestClient, venue: Venue) -> None:
    assert client.post("/events", json=_payload(venue)).status_code == 401


def test_create_requires_organizer_profile(
    client: TestClient, make_user: UserFactory, venue: Venue
) -> None:
    _, headers = make_user(organizer=False)

    assert client.post("/events", json=_payload(venue), headers=headers).status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_at": _iso(9)},  # ends before it starts
        {"registration_closes_at": _iso(11)},  # registration closes after the start
        {"start_at": "2030-01-01T10:00:00"},  # no timezone
        {"capacity": 0},
        {"capacity": 1_000_001},  # above the cap, would overflow the Integer column
        {"title": ""},
    ],
)
def test_create_rejects_invalid_input(
    client: TestClient, org: Headers, venue: Venue, overrides: dict
) -> None:
    response = client.post("/events", json=_payload(venue, **overrides), headers=org)

    assert response.status_code == 422


def test_create_rejects_unknown_venue(client: TestClient, org: Headers, venue: Venue) -> None:
    response = client.post("/events", json=_payload(venue, venue_id=str(uuid.uuid4())), headers=org)

    assert response.status_code == 422


# --- edit / ownership ---------------------------------------------------------------------


def test_owner_can_edit_event(client: TestClient, org: Headers, venue: Venue) -> None:
    event = _create(client, org, venue)

    response = client.patch(f"/events/{event['id']}", json={"title": "New title"}, headers=org)

    assert response.status_code == 200
    assert response.json()["title"] == "New title"
    assert response.json()["category"] == "music"  # untouched fields stay


def test_edit_rejects_capacity_above_cap(client: TestClient, org: Headers, venue: Venue) -> None:
    event = _create(client, org, venue)
    url = f"/events/{event['id']}"

    assert client.patch(url, json={"capacity": 1_000_001}, headers=org).status_code == 422
    assert client.patch(url, json={"capacity": 9_999_999_999}, headers=org).status_code == 422
    assert client.patch(url, json={"capacity": 1_000_000}, headers=org).status_code == 200


def test_edit_rechecks_dates_against_stored_values(
    client: TestClient, org: Headers, venue: Venue
) -> None:
    event = _create(client, org, venue)

    # end_at alone looks fine, but it is before the stored start_at.
    response = client.patch(f"/events/{event['id']}", json={"end_at": _iso(5)}, headers=org)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("patch", "", {"title": "Hijacked"}),
        ("post", "/publish", None),
        ("post", "/cancel", None),
        ("post", "/duplicate", None),
    ],
)
def test_other_organizer_cannot_touch_event(
    client: TestClient,
    org: Headers,
    other_org: Headers,
    venue: Venue,
    method: str,
    suffix: str,
    body: dict | None,
) -> None:
    event = _create(client, org, venue)

    response = client.request(
        method, f"/events/{event['id']}{suffix}", json=body, headers=other_org
    )

    assert response.status_code == 404  # existence is not revealed


# --- status transitions ----------------------------------------------------------------------


def test_publish_unpublish_cancel_flow(client: TestClient, org: Headers, venue: Venue) -> None:
    event = _create(client, org, venue)
    url = f"/events/{event['id']}"

    assert _publish(client, org, event["id"])["status"] == "published"
    assert client.post(f"{url}/publish", headers=org).status_code == 409

    assert client.post(f"{url}/unpublish", headers=org).json()["status"] == "draft"
    assert client.post(f"{url}/unpublish", headers=org).status_code == 409

    assert client.post(f"{url}/cancel", headers=org).json()["status"] == "cancelled"
    assert client.post(f"{url}/cancel", headers=org).status_code == 409
    assert client.patch(url, json={"title": "x"}, headers=org).status_code == 409


def test_cannot_publish_an_event_that_already_ended(
    client: TestClient, org: Headers, venue: Venue
) -> None:
    event = _create(
        client, org, venue, start_at=_iso(-3), end_at=_iso(-2), registration_closes_at=None
    )
    _add_type(client, org, event["id"])

    assert client.post(f"/events/{event['id']}/publish", headers=org).status_code == 409


def test_publish_requires_a_visible_ticket_type(
    client: TestClient, org: Headers, venue: Venue
) -> None:
    event = _create(client, org, venue)
    url = f"/events/{event['id']}/publish"

    assert client.post(url, headers=org).status_code == 409
    _add_type(client, org, event["id"], name="Staff only", is_hidden=True)
    assert client.post(url, headers=org).status_code == 409

    _add_type(client, org, event["id"])
    response = client.post(url, headers=org)
    assert response.status_code == 200
    assert response.json()["status"] == "published"


# --- public list & page ---------------------------------------------------------------------


def test_public_list_shows_only_published_public_events(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    _create(client, org, venue, title=f"Draft stays hidden {tag}")
    published = _create(client, org, venue, title=f"Visible {tag}")
    _publish(client, org, published["id"])
    unlisted = _create(client, org, venue, title=f"Unlisted {tag}", visibility="unlisted")
    _publish(client, org, unlisted["id"])
    private = _create(client, org, venue, title=f"Private {tag}", visibility="private")
    _publish(client, org, private["id"])

    assert _titles(client, tag) == [f"Visible {tag}"]


def test_public_list_filters_and_paginates(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    rows = [("Jazz night", "music"), ("Rock night", "music"), ("Hackathon", "tech")]
    for index, (title, category) in enumerate(rows):
        event = _create(
            client,
            org,
            venue,
            title=f"{title} {tag}",
            category=f"{category}-{tag}",
            start_at=_iso(10 + index),
            end_at=None,
        )
        _publish(client, org, event["id"])

    assert _titles(client, tag) == [f"Jazz night {tag}", f"Rock night {tag}", f"Hackathon {tag}"]
    assert _titles(client, tag, category=f"tech-{tag}") == [f"Hackathon {tag}"]

    page = client.get("/events", params={"q": tag, "limit": 2, "offset": 2}).json()
    assert page["total"] == 3
    assert [e["title"] for e in page["items"]] == [f"Hackathon {tag}"]
    assert client.get("/events", params={"limit": 101}).status_code == 422


def test_public_list_hides_past_events_unless_asked(
    client: TestClient, org: Headers, db: Session, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Old {tag}")
    _publish(client, org, event["id"])
    stored = db.get(Event, uuid.UUID(event["id"]))
    stored.start_at = datetime.now(UTC) - timedelta(days=3)
    stored.end_at = datetime.now(UTC) - timedelta(days=2)
    stored.registration_closes_at = None
    db.commit()

    assert _titles(client, tag) == []
    # from_date only narrows the list, it does not switch the "hide past" filter off.
    assert _titles(client, tag, from_date="2000-01-01T00:00:00Z") == []
    assert _titles(client, tag, include_past=True) == [f"Old {tag}"]


def test_public_page_visibility_rules(client: TestClient, org: Headers, venue: Venue) -> None:
    draft = _create(client, org, venue)
    unlisted = _create(client, org, venue, visibility="unlisted")
    _publish(client, org, unlisted["id"])
    private = _create(client, org, venue, visibility="private")
    _publish(client, org, private["id"])
    cancelled = _create(client, org, venue)
    _publish(client, org, cancelled["id"])
    client.post(f"/events/{cancelled['id']}/cancel", headers=org)

    assert client.get(f"/events/{unlisted['id']}").status_code == 200  # by direct link
    assert client.get(f"/events/{draft['id']}").status_code == 404
    assert client.get(f"/events/{private['id']}").status_code == 404
    assert client.get(f"/events/{cancelled['id']}").status_code == 404
    assert client.get(f"/events/{uuid.uuid4()}").status_code == 404


# --- duplicate -----------------------------------------------------------------------------


def test_duplicate_copies_configuration_into_a_new_draft(
    client: TestClient, org: Headers, db: Session, venue: Venue
) -> None:
    original = _create(client, org, venue, title="Annual meetup")
    _publish(client, org, original["id"])
    db.get(Event, uuid.UUID(original["id"])).paid_sales_active = True
    db.commit()

    response = client.post(f"/events/{original['id']}/duplicate", headers=org)

    assert response.status_code == 201
    copy = response.json()
    assert copy["id"] != original["id"]
    assert copy["status"] == "draft"
    assert copy["paid_sales_active"] is False
    assert (copy["title"], copy["venue_id"], copy["capacity"]) == (
        original["title"],
        original["venue_id"],
        original["capacity"],
    )


def test_duplicate_copies_ticket_types_without_sales(
    client: TestClient, org: Headers, db: Session, make_user: UserFactory, venue: Venue
) -> None:
    original = _create(client, org, venue, refund_policy="Refund up to 24 h before the start.")
    sales_end = _iso(5)
    vip = _add_type(
        client,
        org,
        original["id"],
        name="VIP",
        quantity_total=5,
        max_per_order=2,
        sales_end_at=sales_end,
    )
    _add_type(client, org, original["id"], name="Crew", quantity_total=3, is_hidden=True)
    _add_order(db, make_user()[0], original["id"], OrderStatus.FREE_CONFIRMED)
    order = db.query(Order).filter(Order.event_id == uuid.UUID(original["id"])).one()
    db.add(
        OrderItem(
            order_id=order.id, ticket_type_id=uuid.UUID(vip["id"]), quantity=2, unit_price_kzt=0
        )
    )
    db.commit()

    copy = client.post(f"/events/{original['id']}/duplicate", headers=org).json()
    assert copy["refund_policy"] == "Refund up to 24 h before the start."
    types = client.get(f"/events/{copy['id']}/ticket-types", headers=org).json()

    by_name = {t["name"]: t for t in types}
    assert set(by_name) == {"VIP", "Crew"}
    assert by_name["VIP"]["id"] != vip["id"]
    assert (by_name["VIP"]["quantity_total"], by_name["VIP"]["max_per_order"]) == (5, 2)
    assert datetime.fromisoformat(by_name["VIP"]["sales_end_at"]) == datetime.fromisoformat(
        sales_end
    )
    assert (by_name["VIP"]["sold"], by_name["VIP"]["available"]) == (0, 5)
    assert by_name["Crew"]["is_hidden"] is True
    # The copy can be published right away: it has a visible ticket type.
    assert client.post(f"/events/{copy['id']}/publish", headers=org).status_code == 200


# --- capacity -------------------------------------------------------------------------------


def test_capacity_cannot_drop_below_ticket_types(
    client: TestClient, org: Headers, venue: Venue
) -> None:
    event = _create(client, org, venue, capacity=100)
    _add_type(client, org, event["id"], quantity_total=60)
    url = f"/events/{event['id']}"

    too_low = client.patch(url, json={"capacity": 59}, headers=org)
    assert too_low.status_code == 409
    assert too_low.json()["code"] == "CAPACITY_BELOW_TICKET_QUANTITY"
    assert client.patch(url, json={"capacity": 60}, headers=org).json()["capacity"] == 60
    assert client.patch(url, json={"capacity": None}, headers=org).status_code == 200


def test_capacity_cannot_drop_below_tickets_sold(
    client: TestClient, org: Headers, db: Session, make_user: UserFactory, venue: Venue
) -> None:
    """Old rows may hold more than the types allow; sold tickets still bound capacity."""
    event = _create(client, org, venue, capacity=100)
    ticket_type = _add_type(client, org, event["id"], quantity_total=10)
    _add_order(db, make_user()[0], event["id"], OrderStatus.PAID)
    order = db.query(Order).filter(Order.event_id == uuid.UUID(event["id"])).one()
    db.add(
        OrderItem(
            order_id=order.id,
            ticket_type_id=uuid.UUID(ticket_type["id"]),
            quantity=30,
            unit_price_kzt=0,
        )
    )
    db.commit()
    url = f"/events/{event['id']}"

    response = client.patch(url, json={"capacity": 20}, headers=org)
    assert response.status_code == 409
    assert "sold" in response.json()["detail"]
    assert response.json()["code"] == "CAPACITY_BELOW_SOLD"
    assert client.patch(url, json={"capacity": 30}, headers=org).status_code == 200


# --- organizer dashboard ----------------------------------------------------------------------


def test_organizer_list_buckets(
    client: TestClient, org: Headers, other_org: Headers, db: Session, venue: Venue
) -> None:
    _create(client, org, venue, title="Upcoming")
    active = _create(client, org, venue, title="Active")
    finished = _create(client, org, venue, title="Finished")
    cancelled = _create(client, org, venue, title="Cancelled")
    _create(client, other_org, venue, title="Not mine")

    now = datetime.now(UTC)
    for event, start, end in (
        (active, now - timedelta(hours=1), now + timedelta(hours=1)),
        (finished, now - timedelta(days=2), now - timedelta(days=1)),
    ):
        row = db.get(Event, uuid.UUID(event["id"]))
        row.start_at, row.end_at, row.registration_closes_at = start, end, None
    db.get(Event, uuid.UUID(cancelled["id"])).status = EventStatus.CANCELLED
    db.commit()

    def titles(bucket: str | None = None) -> set[str]:
        params = {"bucket": bucket} if bucket else {}
        response = client.get("/organizer/events", params=params, headers=org)
        assert response.status_code == 200
        return {e["title"] for e in response.json()["items"]}

    assert titles() == {"Upcoming", "Active", "Finished", "Cancelled"}
    assert titles("upcoming") == {"Upcoming"}
    assert titles("active") == {"Active"}
    assert titles("completed") == {"Finished"}
    assert titles("cancelled") == {"Cancelled"}


def test_organizer_buckets_order(
    client: TestClient, org: Headers, db: Session, venue: Venue
) -> None:
    """BE-37 (UX #22): upcoming and active soonest first, completed and cancelled newest
    first, everything together newest first."""
    now = datetime.now(UTC)
    days = {
        "Up +20": 20,
        "Up +5": 5,
        "Up +10": 10,
        "Past -10": -10,
        "Past -2": -2,
        "Past -30": -30,
        "Cancelled +3": 3,
        "Cancelled +15": 15,
    }
    for title, offset in days.items():
        event = _create(client, org, venue, title=title)
        row = db.get(Event, uuid.UUID(event["id"]))
        row.start_at = now + timedelta(days=offset)
        row.end_at = row.start_at + timedelta(hours=2)
        row.registration_closes_at = None
        if title.startswith("Cancelled"):
            row.status = EventStatus.CANCELLED
    for title, start in (("Active -3h", -3), ("Active -1h", -1)):
        event = _create(client, org, venue, title=title)
        row = db.get(Event, uuid.UUID(event["id"]))
        row.start_at = now + timedelta(hours=start)
        row.end_at = now + timedelta(hours=5)
        row.registration_closes_at = None
    db.commit()

    def titles(bucket: str | None = None) -> list[str]:
        params = {"bucket": bucket} if bucket else {}
        response = client.get("/organizer/events", params=params, headers=org)
        assert response.status_code == 200
        return [e["title"] for e in response.json()["items"]]

    assert titles("upcoming") == ["Up +5", "Up +10", "Up +20"]
    assert titles("active") == ["Active -3h", "Active -1h"]
    assert titles("completed") == ["Past -2", "Past -10", "Past -30"]
    assert titles("cancelled") == ["Cancelled +15", "Cancelled +3"]
    assert titles()[:3] == ["Up +20", "Cancelled +15", "Up +10"]


def test_organizer_can_open_own_event_in_any_status(
    client: TestClient, org: Headers, other_org: Headers, venue: Venue
) -> None:
    event = _create(client, org, venue)  # still a draft

    assert client.get(f"/organizer/events/{event['id']}", headers=org).status_code == 200
    assert client.get(f"/organizer/events/{event['id']}", headers=other_org).status_code == 404
    assert client.get("/organizer/events").status_code == 401


# --- rules that depend on orders and on the published state (review of PR #5) ---------------


def _add_order(
    db: Session,
    buyer: User,
    event_id: str,
    status: OrderStatus,
    created_at: datetime | None = None,
) -> None:
    order = Order(buyer_user_id=buyer.id, event_id=uuid.UUID(event_id), status=status)
    if created_at is not None:
        order.created_at = created_at
    db.add(order)
    db.commit()


@pytest.mark.parametrize(
    "order_status", [OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.FREE_CONFIRMED]
)
def test_cannot_unpublish_event_with_orders(
    client: TestClient,
    db: Session,
    make_user: UserFactory,
    org: Headers,
    venue: Venue,
    order_status: OrderStatus,
) -> None:
    event = _publish(client, org, _create(client, org, venue)["id"])
    _add_order(db, make_user()[0], event["id"], order_status)

    response = client.post(f"/events/{event['id']}/unpublish", headers=org)

    assert response.status_code == 409
    assert client.get(f"/events/{event['id']}").status_code == 200  # still public


def test_cancelled_orders_do_not_block_unpublish(
    client: TestClient, db: Session, make_user: UserFactory, org: Headers, venue: Venue
) -> None:
    event = _publish(client, org, _create(client, org, venue)["id"])
    _add_order(db, make_user()[0], event["id"], OrderStatus.CANCELLED)

    response = client.post(f"/events/{event['id']}/unpublish", headers=org)

    assert response.status_code == 200


def test_expired_pending_order_does_not_block_update_or_unpublish(
    client: TestClient, db: Session, make_user: UserFactory, org: Headers, venue: Venue
) -> None:
    """An abandoned checkout (older than the 15-minute hold) holds nothing, so it must not
    lock the venue or keep the event published."""
    event = _publish(client, org, _create(client, org, venue)["id"])
    other_venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}", city="Astana")
    db.add(other_venue)
    db.commit()
    expired = datetime.now(UTC) - timedelta(minutes=16)
    _add_order(db, make_user()[0], event["id"], OrderStatus.PENDING, created_at=expired)

    url = f"/events/{event['id']}"
    assert client.patch(url, json={"venue_id": str(other_venue.id)}, headers=org).status_code == 200
    assert client.post(f"{url}/unpublish", headers=org).status_code == 200


def test_published_event_cannot_be_moved_into_the_past(
    client: TestClient, org: Headers, venue: Venue
) -> None:
    event = _publish(client, org, _create(client, org, venue)["id"])

    response = client.patch(
        f"/events/{event['id']}", json={"start_at": _iso(-2), "end_at": _iso(-1)}, headers=org
    )

    assert response.status_code == 409


def test_draft_event_may_still_get_past_dates(
    client: TestClient, org: Headers, venue: Venue
) -> None:
    event = _create(client, org, venue)

    response = client.patch(
        f"/events/{event['id']}", json={"start_at": _iso(-2), "end_at": _iso(-1)}, headers=org
    )

    assert response.status_code == 200  # publish will refuse it later


def test_venue_and_seating_are_locked_once_orders_exist(
    client: TestClient,
    db: Session,
    make_user: UserFactory,
    org: Headers,
    venue: Venue,
) -> None:
    event = _publish(client, org, _create(client, org, venue)["id"])
    other_venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}", city="Astana")
    db.add(other_venue)
    db.commit()
    url = f"/events/{event['id']}"

    assert client.patch(url, json={"venue_id": str(other_venue.id)}, headers=org).status_code == 200
    assert client.patch(url, json={"venue_id": str(venue.id)}, headers=org).status_code == 200

    _add_order(db, make_user()[0], event["id"], OrderStatus.PAID)

    assert client.patch(url, json={"venue_id": str(other_venue.id)}, headers=org).status_code == 409
    assert client.patch(url, json={"seating_type": "assigned"}, headers=org).status_code == 409
    # Re-sending the current value or editing other fields is still fine.
    assert client.patch(url, json={"venue_id": str(venue.id)}, headers=org).status_code == 200
    assert client.patch(url, json={"title": "Renamed"}, headers=org).status_code == 200


@pytest.mark.parametrize("field", ["title", "venue_id", "start_at", "visibility", "seating_type"])
def test_required_fields_cannot_be_set_to_null(
    client: TestClient, org: Headers, venue: Venue, field: str
) -> None:
    event = _create(client, org, venue)

    response = client.patch(f"/events/{event['id']}", json={field: None}, headers=org)

    assert response.status_code == 422


def test_optional_fields_can_be_cleared(client: TestClient, org: Headers, venue: Venue) -> None:
    event = _create(client, org, venue)

    response = client.patch(
        f"/events/{event['id']}", json={"description": None, "end_at": None}, headers=org
    )

    assert response.status_code == 200
    assert response.json()["description"] is None
    assert response.json()["end_at"] is None


def test_search_treats_like_wildcards_literally(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    _publish(client, org, _create(client, org, venue, title=f"{tag} 100% fun")["id"])
    _publish(client, org, _create(client, org, venue, title=f"{tag} plain")["id"])

    assert _titles(client, f"{tag} 100%") == [f"{tag} 100% fun"]
    # Unescaped, "%" and "_" would act as wildcards and match both events.
    assert _titles(client, f"{tag}%") == []
    assert _titles(client, f"{tag}_") == []


def test_list_filters_require_timezone(client: TestClient) -> None:
    response = client.get("/events", params={"from_date": "2030-01-01T00:00:00"})

    assert response.status_code == 422


# --- slugs (BE-35): every title carries the tag, so slugs stay unique ---------------


def test_new_event_gets_a_slug_from_its_title(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Джаз-вечер Алматы {tag}")
    assert event["slug"] == f"dzhaz-vecher-almaty-{tag}"


def test_same_title_gets_numbered_slugs(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    title = f"Концерт {tag}"
    found = [_create(client, org, venue, title=title)["slug"] for _ in range(3)]
    assert found == [f"kontsert-{tag}", f"kontsert-{tag}-2", f"kontsert-{tag}-3"]


def test_public_event_opens_by_slug_and_by_id(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Open {tag}")
    _publish(client, org, event["id"])

    by_slug = client.get(f"/events/{event['slug']}")
    assert by_slug.status_code == 200, by_slug.text
    assert by_slug.json()["id"] == event["id"]
    by_id = client.get(f"/events/{event['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["slug"] == event["slug"]


def test_unlisted_event_has_a_slug(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Unlisted {tag}", visibility="unlisted")
    _publish(client, org, event["id"])
    assert client.get(f"/events/unlisted-{tag}").status_code == 200


def test_draft_is_not_found_by_slug(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Draft {tag}")
    assert event["slug"] == f"draft-{tag}"
    assert client.get(f"/events/{event['slug']}").status_code == 404
    assert client.get(f"/events/no-such-event-{tag}").status_code == 404


def test_private_event_has_no_slug(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Private {tag}", visibility="private")
    assert event["slug"] is None
    _publish(client, org, event["id"])
    assert client.get(f"/events/private-{tag}").status_code == 404
    assert client.get(f"/events/{event['id']}").status_code == 404  # as before BE-35


def test_slug_survives_a_title_edit(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"First {tag}")
    _publish(client, org, event["id"])
    edited = client.patch(f"/events/{event['id']}", json={"title": f"Second {tag}"}, headers=org)
    assert edited.status_code == 200, edited.text
    assert edited.json()["slug"] == f"first-{tag}"
    assert client.get(f"/events/first-{tag}").json()["title"] == f"Second {tag}"


def test_going_private_drops_the_slug_and_back_gets_one(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Switch {tag}")
    _publish(client, org, event["id"])

    private = client.patch(f"/events/{event['id']}", json={"visibility": "private"}, headers=org)
    assert private.json()["slug"] is None
    assert client.get(f"/events/switch-{tag}").status_code == 404

    public = client.patch(f"/events/{event['id']}", json={"visibility": "public"}, headers=org)
    assert public.json()["slug"] == f"switch-{tag}"


def test_duplicate_gets_its_own_slug(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Copy me {tag}")
    copy = client.post(f"/events/{event['id']}/duplicate", headers=org)
    assert copy.status_code == 201, copy.text
    assert copy.json()["slug"] == f"copy-me-{tag}-2"
    assert client.get(f"/organizer/events/{event['id']}", headers=org).json()["slug"] == (
        f"copy-me-{tag}"
    )


def test_list_items_carry_the_slug(
    client: TestClient, org: Headers, venue: Venue, tag: str
) -> None:
    event = _create(client, org, venue, title=f"Listed {tag}")
    _publish(client, org, event["id"])
    items = client.get("/events", params={"q": tag}).json()["items"]
    assert [e["slug"] for e in items] == [f"listed-{tag}"]
