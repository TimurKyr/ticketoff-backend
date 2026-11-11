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
from app.modules.orders.models import Order

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
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash=hash_password("x" * 12))
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


def _publish(client: TestClient, headers: Headers, event_id: str) -> dict:
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

    assert client.post(f"/events/{event['id']}/publish", headers=org).status_code == 409


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
