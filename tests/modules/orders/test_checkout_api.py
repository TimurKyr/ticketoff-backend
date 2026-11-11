"""POST /events/{event_id}/checkout (BE-14) over HTTP, on the rolled-back test session."""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.orders.models import TicketType
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
)
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def login(api: TestClient) -> Callable[[], Headers]:
    def make() -> Headers:
        email, password = f"user-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"
        api.post("/auth/register", json={"email": email, "password": password})
        token = api.post("/auth/login", data={"username": email, "password": password}).json()[
            "access_token"
        ]
        return {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def event(db_session: Session) -> Event:
    event = create_event(db_session, create_organizer(db_session))
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, price_kzt=15000, quantity_total=10)


def post_checkout(
    api: TestClient, headers: Headers, event: Event, *items: tuple[TicketType, int], **extra
):
    total = sum(q for _, q in items)
    body = {
        "items": [{"ticket_type_id": str(tt.id), "quantity": q} for tt, q in items],
        "attendees": [{"name": f"Guest {n}", "email": None} for n in range(total)],
    } | extra
    return api.post(f"/events/{event.id}/checkout", json=body, headers=headers)


def test_requires_login(api: TestClient, event: Event) -> None:
    assert api.post(f"/events/{event.id}/checkout", json={}).status_code == 401


def test_checkout_returns_201_pending_order(
    api: TestClient, login: Callable[[], Headers], event: Event, paid_type: TicketType
) -> None:
    response = post_checkout(api, login(), event, (paid_type, 2))

    assert response.status_code == 201
    body = response.json()
    assert (body["status"], body["total_kzt"], body["tickets"]) == ("pending", 30000, [])
    assert body["items"] == [
        {"ticket_type_id": str(paid_type.id), "quantity": 2, "unit_price_kzt": 15000}
    ]
    assert body["expires_at"] is not None


def test_client_prices_are_ignored(
    api: TestClient, login: Callable[[], Headers], event: Event, paid_type: TicketType
) -> None:
    body = {
        "items": [{"ticket_type_id": str(paid_type.id), "quantity": 1, "unit_price_kzt": 1}],
        "attendees": [{"name": "Aruzhan"}],
        "total_kzt": 1,
    }

    response = api.post(f"/events/{event.id}/checkout", json=body, headers=login())

    assert response.status_code == 201
    assert response.json()["total_kzt"] == 15000
    assert response.json()["items"][0]["unit_price_kzt"] == 15000


def test_expires_at_shows_on_my_orders(
    api: TestClient, login: Callable[[], Headers], event: Event, paid_type: TicketType
) -> None:
    headers = login()
    created = post_checkout(api, headers, event, (paid_type, 1)).json()

    one = api.get(f"/orders/{created['id']}", headers=headers).json()
    [listed] = api.get("/orders/me", headers=headers).json()

    assert one["expires_at"] == listed["expires_at"] == created["expires_at"]
    # Roughly 15 minutes from now (created_at is the DB's transaction time).
    remaining = datetime.fromisoformat(created["expires_at"]) - datetime.now().astimezone()
    assert timedelta(minutes=13) < remaining <= timedelta(minutes=15)


def test_free_registration_has_null_expires_at(
    api: TestClient, login: Callable[[], Headers], db_session: Session, event: Event
) -> None:
    free_type = create_free_ticket_type(db_session, event)
    body = {
        "items": [{"ticket_type_id": str(free_type.id), "quantity": 1}],
        "attendees": [{"name": "A"}],
    }

    response = api.post(f"/events/{event.id}/registrations", json=body, headers=login())

    assert response.status_code == 201
    assert response.json()["expires_at"] is None


def test_paid_before_activation_is_409(
    api: TestClient,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
    paid_type: TicketType,
) -> None:
    event.paid_sales_active = False
    db_session.flush()

    response = post_checkout(api, login(), event, (paid_type, 1))

    assert response.status_code == 409
    assert response.json()["detail"] == "Paid ticket sales are not active for this event"


def test_free_type_is_409(
    api: TestClient, login: Callable[[], Headers], db_session: Session, event: Event
) -> None:
    free_type = create_free_ticket_type(db_session, event)

    response = post_checkout(api, login(), event, (free_type, 1))

    assert response.status_code == 409
    assert response.json()["detail"] == "Use registration for free tickets"


def test_hidden_type_is_404(
    api: TestClient, login: Callable[[], Headers], db_session: Session, event: Event
) -> None:
    hidden = create_ticket_type(db_session, event, is_hidden=True)

    assert post_checkout(api, login(), event, (hidden, 1)).status_code == 404


def test_unknown_event_is_404(
    api: TestClient, login: Callable[[], Headers], paid_type: TicketType
) -> None:
    body = {
        "items": [{"ticket_type_id": str(paid_type.id), "quantity": 1}],
        "attendees": [{"name": "A"}],
    }

    response = api.post(f"/events/{uuid.uuid4()}/checkout", json=body, headers=login())

    assert response.status_code == 404


def test_attendee_count_mismatch_is_422(
    api: TestClient, login: Callable[[], Headers], event: Event, paid_type: TicketType
) -> None:
    response = post_checkout(api, login(), event, (paid_type, 2), attendees=[{"name": "Only one"}])

    assert response.status_code == 422
