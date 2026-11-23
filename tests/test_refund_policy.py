"""Event refund policy (BE-19): organizers set it, buyers read it on the public page."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.models import Venue
from app.modules.events.schemas import REFUND_POLICY_MAX_LENGTH
from tests.emails import confirm_email

Headers = dict[str, str]
POLICY = "Full refund up to 48 hours before the start. Возврат не позднее чем за 48 часов."


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def organizer(api: TestClient) -> Headers:
    email, password = f"org-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"
    assert (
        api.post("/auth/register", json={"email": email, "password": password}).status_code == 201
    )
    confirm_email(api, email)
    token = api.post("/auth/login", data={"username": email, "password": password}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        api.post("/organizer/profile", json={"display_name": "Org"}, headers=headers).status_code
        == 201
    )
    return headers


@pytest.fixture
def venue(db_session: Session) -> Venue:
    venue = Venue(name="Hall")
    db_session.add(venue)
    db_session.flush()
    return venue


@pytest.fixture
def create(api: TestClient, organizer: Headers, venue: Venue) -> Callable[..., object]:
    def make(**fields: object):
        body = {
            "title": "Concert",
            "venue_id": str(venue.id),
            "start_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        } | fields
        return api.post("/events", json=body, headers=organizer)

    return make


def test_policy_is_saved_and_shown_on_the_public_page(
    api: TestClient, organizer: Headers, create: Callable[..., object]
) -> None:
    created = create(refund_policy=POLICY)
    assert created.status_code == 201
    event = created.json()
    assert event["refund_policy"] == POLICY

    # Publishing needs a visible ticket type (BE-11, #17).
    ticket_type = {"name": "Free", "kind": "free", "price_kzt": 0, "quantity_total": 10}
    assert (
        api.post(f"/events/{event['id']}/ticket-types", json=ticket_type, headers=organizer)
    ).status_code == 201
    assert api.post(f"/events/{event['id']}/publish", headers=organizer).status_code == 200
    public = api.get(f"/events/{event['id']}")  # no login: what a buyer sees
    assert public.status_code == 200
    assert public.json()["refund_policy"] == POLICY


def test_policy_is_optional(create: Callable[..., object]) -> None:
    created = create()

    assert created.status_code == 201
    assert created.json()["refund_policy"] is None


def test_policy_can_be_changed_and_cleared(
    api: TestClient, organizer: Headers, create: Callable[..., object]
) -> None:
    event_id = create(refund_policy=POLICY).json()["id"]

    changed = api.patch(
        f"/events/{event_id}", json={"refund_policy": "No refunds."}, headers=organizer
    )
    assert (changed.status_code, changed.json()["refund_policy"]) == (200, "No refunds.")

    untouched = api.patch(f"/events/{event_id}", json={"title": "Renamed"}, headers=organizer)
    assert untouched.json()["refund_policy"] == "No refunds."  # not sent, not changed

    cleared = api.patch(f"/events/{event_id}", json={"refund_policy": None}, headers=organizer)
    assert (cleared.status_code, cleared.json()["refund_policy"]) == (200, None)


def test_policy_longer_than_the_limit_is_422(
    api: TestClient, organizer: Headers, create: Callable[..., object]
) -> None:
    too_long = "x" * (REFUND_POLICY_MAX_LENGTH + 1)

    assert create(refund_policy="x" * REFUND_POLICY_MAX_LENGTH).status_code == 201
    response = create(refund_policy=too_long)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "refund_policy"]

    event_id = create().json()["id"]
    patch = api.patch(f"/events/{event_id}", json={"refund_policy": too_long}, headers=organizer)
    assert patch.status_code == 422
