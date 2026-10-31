import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.orders.models import TicketType
from app.modules.orders.qr import verify_ticket_token
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
)

Headers = dict[str, str]


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    """Client whose requests use the rolled-back test session, so nothing persists."""
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def login(api: TestClient) -> Callable[[], Headers]:
    """Register a fresh user through the API and return their auth headers."""

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
def event(db_session: Session) -> Event:
    return create_event(db_session, create_organizer(db_session))


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event, quantity_total=5)


def register(
    api: TestClient, headers: Headers, event: Event, *items: tuple[TicketType, int], **extra: object
):
    body = {"items": [{"ticket_type_id": str(tt.id), "quantity": q} for tt, q in items]} | extra
    return api.post(f"/events/{event.id}/registrations", json=body, headers=headers)


# --- auth ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", f"/events/{uuid.uuid4()}/registrations"),
        ("get", "/orders/me"),
        ("get", f"/orders/{uuid.uuid4()}"),
        ("get", f"/tickets/{uuid.uuid4()}"),
    ],
)
def test_requires_login(api: TestClient, method: str, path: str) -> None:
    assert api.request(method, path, json={}).status_code == 401


# --- registration ---


def test_register_returns_201_with_signed_tickets(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType
) -> None:
    attendees = [{"name": "Aruzhan", "email": "a@example.com"}, {"name": "Dias", "email": None}]

    response = register(api, login(), event, (free_type, 2), attendees=attendees)

    assert response.status_code == 201
    body = response.json()
    assert body["event_id"] == str(event.id)
    assert (body["status"], body["total_kzt"]) == ("free_confirmed", 0)
    assert body["items"] == [
        {"ticket_type_id": str(free_type.id), "quantity": 2, "unit_price_kzt": 0}
    ]
    assert sorted(t["attendee"]["name"] for t in body["tickets"]) == ["Aruzhan", "Dias"]
    for ticket in body["tickets"]:
        assert ticket["status"] == "valid"
        claims = verify_ticket_token(ticket["qr_token"])
        assert claims is not None
        assert str(claims.ticket_id) == ticket["id"]


@pytest.mark.parametrize(
    "extra",
    [
        {"attendees": [{"name": "Only one", "email": None}]},
        {"attendees": [{"name": "A", "email": "not-an-email"}, {"name": "B"}]},
    ],
)
def test_invalid_body_is_422(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType, extra: dict
) -> None:
    assert register(api, login(), event, (free_type, 2), **extra).status_code == 422


def test_duplicate_ticket_type_is_422(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType
) -> None:
    assert register(api, login(), event, (free_type, 1), (free_type, 1)).status_code == 422


def test_unknown_event_is_404(
    api: TestClient, login: Callable[[], Headers], free_type: TicketType
) -> None:
    response = api.post(
        f"/events/{uuid.uuid4()}/registrations",
        json={"items": [{"ticket_type_id": str(free_type.id), "quantity": 1}]},
        headers=login(),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Event not found"


def test_paid_ticket_type_is_409(
    api: TestClient, login: Callable[[], Headers], db_session: Session, event: Event
) -> None:
    response = register(api, login(), event, (create_ticket_type(db_session, event), 1))
    assert response.status_code == 409
    assert response.json()["detail"] == "Use checkout for paid tickets"


def test_sold_out_is_409(
    api: TestClient, login: Callable[[], Headers], db_session: Session, event: Event
) -> None:
    last_one = create_free_ticket_type(db_session, event, quantity_total=1)
    assert register(api, login(), event, (last_one, 1)).status_code == 201

    response = register(api, login(), event, (last_one, 1))

    assert response.status_code == 409
    assert response.json()["detail"].startswith("Not enough tickets left")


# --- reads ---


def test_my_orders_lists_only_mine(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType
) -> None:
    me, someone_else = login(), login()
    mine = {register(api, me, event, (free_type, 1)).json()["id"] for _ in range(2)}
    register(api, someone_else, event, (free_type, 1))

    response = api.get("/orders/me", headers=me)

    assert response.status_code == 200
    assert {order["id"] for order in response.json()} == mine


def test_get_my_order(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType
) -> None:
    headers = login()
    created = register(api, headers, event, (free_type, 2)).json()

    response = api.get(f"/orders/{created['id']}", headers=headers)

    assert response.status_code == 200
    assert response.json() == created


def test_get_my_ticket(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType
) -> None:
    headers = login()
    [ticket] = register(api, headers, event, (free_type, 1)).json()["tickets"]

    response = api.get(f"/tickets/{ticket['id']}", headers=headers)

    assert response.status_code == 200
    assert response.json() == ticket


def test_other_users_order_and_ticket_are_404(
    api: TestClient, login: Callable[[], Headers], event: Event, free_type: TicketType
) -> None:
    order = register(api, login(), event, (free_type, 1)).json()
    intruder = login()

    assert api.get(f"/orders/{order['id']}", headers=intruder).status_code == 404
    assert api.get(f"/tickets/{order['tickets'][0]['id']}", headers=intruder).status_code == 404


def test_unknown_order_and_ticket_are_404(api: TestClient, login: Callable[[], Headers]) -> None:
    headers = login()
    assert api.get(f"/orders/{uuid.uuid4()}", headers=headers).status_code == 404
    assert api.get(f"/tickets/{uuid.uuid4()}", headers=headers).status_code == 404


def test_malformed_id_is_422(api: TestClient, login: Callable[[], Headers]) -> None:
    assert api.get("/orders/not-a-uuid", headers=login()).status_code == 422
