import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Row, Seat, VenueSection
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Ticket, TicketType
from tests.factories import create_event, create_free_ticket_type, create_organizer
from tests.markers import postgres_only
from tests.modules.orders.test_ticket_pdf import decode_qr, page_text

# Tickets come from free registration: FOR UPDATE locks and timezone-aware datetimes.
pytestmark = postgres_only

Headers = dict[str, str]
GUEST = "Аружан Сейткали"


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
    """Starts at 14:00 UTC a month from now; the factory venue is in Asia/Almaty (UTC+5)."""
    event = create_event(db_session, create_organizer(db_session))
    start = datetime.now(UTC).replace(hour=14, minute=0, second=0, microsecond=0)
    event.start_at = start + timedelta(days=30)
    db_session.flush()
    assert event.venue.timezone == "Asia/Almaty"
    return event


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event, name="Standard", quantity_total=5)


@pytest.fixture
def buyer(login: Callable[[], Headers]) -> Headers:
    return login()


@pytest.fixture
def ticket(api: TestClient, buyer: Headers, event: Event, free_type: TicketType) -> dict:
    """A ticket registered through the API, as GET /tickets/{id} returns it."""
    response = api.post(
        f"/events/{event.id}/registrations",
        json={
            "items": [{"ticket_type_id": str(free_type.id), "quantity": 1}],
            "attendees": [{"name": GUEST, "email": "aruzhan@example.com"}],
        },
        headers=buyer,
    )
    assert response.status_code == 201, response.text
    ticket_id = response.json()["tickets"][0]["id"]
    return api.get(f"/tickets/{ticket_id}", headers=buyer).json()


def download(api: TestClient, ticket_id: str, headers: Headers):
    return api.get(f"/tickets/{ticket_id}/pdf", headers=headers)


# --- success ---


def test_returns_pdf_with_headers_and_the_tickets_qr(
    api: TestClient, buyer: Headers, ticket: dict
) -> None:
    response = download(api, ticket["id"], buyer)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="ticket-{ticket["id"]}.pdf"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"%PDF-")
    assert decode_qr(response.content)[0] == ticket["qr_token"]


def test_shows_ticket_details_and_venue_local_time(
    api: TestClient, buyer: Headers, ticket: dict, event: Event
) -> None:
    text = page_text(download(api, ticket["id"], buyer).content)

    # 14:00 UTC at a venue in Asia/Almaty (UTC+5).
    assert "19:00 (Asia/Almaty)" in text
    for expected in (event.title, event.venue.name, "Standard", GUEST, ticket["id"]):
        assert expected in text
    assert "aruzhan@example.com" not in text
    assert "Seat" not in text  # general admission


def test_shows_the_seat_when_the_ticket_has_one(
    api: TestClient, buyer: Headers, ticket: dict, event: Event, db_session: Session
) -> None:
    # Registration cannot sell seats yet, so the seat is attached directly.
    section = VenueSection(venue_id=event.venue_id, name="Партер")
    row = Row(section=section, label="3")
    seat = Seat(row=row, number="12")
    db_session.add_all([section, row, seat])
    db_session.flush()
    db_session.get_one(Ticket, uuid.UUID(ticket["id"])).seat_id = seat.id
    db_session.flush()
    db_session.expire_all()  # reload Ticket.seat from the new seat_id

    text = page_text(download(api, ticket["id"], buyer).content)

    assert "Партер · Row 3 · Seat 12" in text


def test_checked_in_ticket_still_downloads(
    api: TestClient, buyer: Headers, ticket: dict, db_session: Session
) -> None:
    db_session.get_one(Ticket, uuid.UUID(ticket["id"])).status = TicketStatus.CHECKED_IN
    db_session.flush()

    assert download(api, ticket["id"], buyer).status_code == 200


def test_completed_event_still_downloads(
    api: TestClient, buyer: Headers, ticket: dict, event: Event, db_session: Session
) -> None:
    event.status = EventStatus.COMPLETED
    db_session.flush()

    assert download(api, ticket["id"], buyer).status_code == 200


def test_downloading_twice_gives_the_same_token_and_no_new_tickets(
    api: TestClient, buyer: Headers, ticket: dict, db_session: Session
) -> None:
    def ticket_count() -> int:
        return db_session.scalar(select(func.count()).select_from(Ticket))

    before = ticket_count()
    first = download(api, ticket["id"], buyer)
    second = download(api, ticket["id"], buyer)

    assert decode_qr(first.content)[0] == decode_qr(second.content)[0] == ticket["qr_token"]
    assert ticket_count() == before
    assert api.get(f"/tickets/{ticket['id']}", headers=buyer).json() == ticket


# --- access ---


def test_requires_login(api: TestClient) -> None:
    assert api.get(f"/tickets/{uuid.uuid4()}/pdf").status_code == 401


def test_other_users_ticket_is_404(
    api: TestClient, login: Callable[[], Headers], ticket: dict
) -> None:
    response = download(api, ticket["id"], login())

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


def test_unknown_ticket_is_404(api: TestClient, buyer: Headers) -> None:
    assert download(api, str(uuid.uuid4()), buyer).status_code == 404


# --- dead tickets ---


@pytest.mark.parametrize("status", [TicketStatus.CANCELLED, TicketStatus.REFUNDED])
def test_cancelled_or_refunded_ticket_is_409(
    api: TestClient, buyer: Headers, ticket: dict, db_session: Session, status: TicketStatus
) -> None:
    db_session.get_one(Ticket, uuid.UUID(ticket["id"])).status = status
    db_session.flush()

    response = download(api, ticket["id"], buyer)

    assert response.status_code == 409
    assert response.json() == {"detail": "Ticket is no longer valid"}


def test_cancelled_event_is_409(
    api: TestClient, buyer: Headers, ticket: dict, event: Event, db_session: Session
) -> None:
    event.status = EventStatus.CANCELLED
    db_session.flush()

    response = download(api, ticket["id"], buyer)

    assert response.status_code == 409
    assert response.json() == {"detail": "Event was cancelled"}


# --- contract ---


def test_openapi_declares_a_pdf_response(api: TestClient) -> None:
    operation = api.get("/openapi.json").json()["paths"]["/tickets/{ticket_id}/pdf"]["get"]

    assert operation["responses"]["200"]["content"] == {
        "application/pdf": {"schema": {"type": "string", "format": "binary"}}
    }
