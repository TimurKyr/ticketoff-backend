"""POST /orders/{order_id}/pay (BE-14) over HTTP, on the rolled-back test session."""

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
from tests.factories import create_event, create_organizer, create_ticket_type
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
def paid_type(db_session: Session) -> TicketType:
    event: Event = create_event(db_session, create_organizer(db_session))
    event.paid_sales_active = True
    db_session.flush()
    return create_ticket_type(db_session, event, price_kzt=15000)


@pytest.fixture
def buyer(login: Callable[[], Headers]) -> Headers:
    return login()


@pytest.fixture
def order_id(api: TestClient, buyer: Headers, paid_type: TicketType) -> str:
    body = {
        "items": [{"ticket_type_id": str(paid_type.id), "quantity": 2}],
        "attendees": [{"name": "Aruzhan"}, {"name": "Dias"}],
    }
    response = api.post(f"/events/{paid_type.event_id}/checkout", json=body, headers=buyer)
    assert response.status_code == 201
    return response.json()["id"]


def pay(api: TestClient, headers: Headers, order_id: str, simulate: str = "success"):
    return api.post(f"/orders/{order_id}/pay", json={"simulate": simulate}, headers=headers)


def test_success_is_200_with_payment_and_tickets(
    api: TestClient, buyer: Headers, order_id: str
) -> None:
    response = pay(api, buyer, order_id)

    assert response.status_code == 200
    body = response.json()
    assert set(body["payment"]) == {"id", "status", "amount_kzt", "is_simulated"}
    assert (body["payment"]["status"], body["payment"]["amount_kzt"]) == ("succeeded", 30000)
    assert body["payment"]["is_simulated"] is True
    assert body["order"]["status"] == "paid"
    assert len(body["order"]["tickets"]) == 2
    for ticket in body["order"]["tickets"]:
        assert str(verify_ticket_token(ticket["qr_token"])) == ticket["id"]
    # The tickets are the buyer's own: readable through the tickets API.
    ticket_id = body["order"]["tickets"][0]["id"]
    assert api.get(f"/tickets/{ticket_id}", headers=buyer).status_code == 200


def test_fail_is_200_with_failed_payment_and_pending_order(
    api: TestClient, buyer: Headers, order_id: str
) -> None:
    response = pay(api, buyer, order_id, "fail")

    assert response.status_code == 200
    body = response.json()
    assert body["payment"]["status"] == "failed"
    assert (body["order"]["status"], body["order"]["tickets"]) == ("pending", [])
    assert body["order"]["expires_at"] is not None


def test_pay_twice_returns_same_tickets(api: TestClient, buyer: Headers, order_id: str) -> None:
    first = pay(api, buyer, order_id).json()
    second = pay(api, buyer, order_id).json()

    assert second["payment"]["id"] == first["payment"]["id"]
    assert {t["id"] for t in second["order"]["tickets"]} == {
        t["id"] for t in first["order"]["tickets"]
    }


@pytest.mark.parametrize("body", [{}, {"simulate": "maybe"}, {"simulate": True}])
def test_invalid_simulate_is_422(
    api: TestClient, buyer: Headers, order_id: str, body: dict
) -> None:
    response = api.post(f"/orders/{order_id}/pay", json=body, headers=buyer)

    assert response.status_code == 422


def test_requires_login(api: TestClient, order_id: str) -> None:
    assert api.post(f"/orders/{order_id}/pay", json={"simulate": "success"}).status_code == 401


def test_other_users_order_is_404(
    api: TestClient, login: Callable[[], Headers], order_id: str
) -> None:
    assert pay(api, login(), order_id).status_code == 404
