"""A confirmed email is required to buy tickets, register for free ones and publish an event
(TZ UC1, R-9). Logging in and everything else still work without it."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.orders.models import TicketType
from tests.emails import confirm_email
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
PASSWORD = "correct-horse-battery"
NOT_VERIFIED = "EMAIL_NOT_VERIFIED"


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def bearer(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def order_body(ticket_type: TicketType) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        "attendees": [{"name": "Guest", "email": None}],
    }


def assert_not_verified(response) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == NOT_VERIFIED
    assert "email" in body["detail"].lower()


@pytest.fixture
def unverified(db_session: Session) -> User:
    return create_user(db_session, email_verified=False)


@pytest.fixture
def event(db_session: Session) -> Event:
    return create_event(db_session, create_organizer(db_session))


def test_checkout_needs_a_confirmed_email(
    api: TestClient, db_session: Session, unverified: User, event: Event
) -> None:
    paid = create_ticket_type(db_session, event)
    response = api.post(
        f"/events/{event.id}/checkout", json=order_body(paid), headers=bearer(unverified)
    )
    assert_not_verified(response)


def test_free_registration_needs_a_confirmed_email(
    api: TestClient, db_session: Session, unverified: User, event: Event
) -> None:
    free = create_free_ticket_type(db_session, event)
    response = api.post(
        f"/events/{event.id}/registrations", json=order_body(free), headers=bearer(unverified)
    )
    assert_not_verified(response)


def test_publish_needs_a_confirmed_email(api: TestClient, db_session: Session) -> None:
    owner = create_user(db_session, email_verified=False)
    organizer = OrganizerProfile(user_id=owner.id, display_name="Unconfirmed")
    db_session.add(organizer)
    db_session.flush()
    draft = create_event(db_session, organizer, status=EventStatus.DRAFT)
    create_ticket_type(db_session, draft)

    assert_not_verified(api.post(f"/events/{draft.id}/publish", headers=bearer(owner)))
    db_session.refresh(draft)
    assert draft.status == EventStatus.DRAFT

    owner.email_verified = True
    db_session.flush()
    response = api.post(f"/events/{draft.id}/publish", headers=bearer(owner))
    assert response.status_code == 200, response.text


def test_someone_elses_event_is_still_404_not_the_email_error(
    api: TestClient, db_session: Session, unverified: User, event: Event
) -> None:
    """Ownership is checked first, so the answer does not reveal that the event exists."""
    db_session.add(OrganizerProfile(user_id=unverified.id, display_name="Other"))
    db_session.flush()
    response = api.post(f"/events/{event.id}/publish", headers=bearer(unverified))
    assert response.status_code == 404
    assert "code" not in response.json()


def test_login_and_me_work_without_a_confirmed_email(api: TestClient) -> None:
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    assert (
        api.post("/auth/register", json={"email": email, "password": PASSWORD}).status_code == 201
    )
    login = api.post("/auth/login", data={"username": email, "password": PASSWORD})
    assert login.status_code == 200
    me = api.get("/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["email_verified"] is False


def test_after_the_link_the_same_token_can_register(
    api: TestClient, db_session: Session, event: Event
) -> None:
    """Confirming takes effect at once: no new login needed (the flag is read per request)."""
    free = create_free_ticket_type(db_session, event)
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    api.post("/auth/register", json={"email": email, "password": PASSWORD})
    token = api.post("/auth/login", data={"username": email, "password": PASSWORD}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/events/{event.id}/registrations"

    assert_not_verified(api.post(url, json=order_body(free), headers=headers))
    confirm_email(api, email)
    response = api.post(url, json=order_body(free), headers=headers)
    assert response.status_code == 201, response.text


def test_other_errors_have_no_code(api: TestClient, unverified: User) -> None:
    """`code` is sent only for errors that have one; the usual body stays {"detail": ...}."""
    response = api.get(f"/orders/{uuid.uuid4()}", headers=bearer(unverified))
    assert response.status_code == 404
    assert response.json() == {"detail": response.json()["detail"]}


def test_openapi_documents_the_403() -> None:
    paths = app.openapi()["paths"]
    for path in (
        "/events/{event_id}/checkout",
        "/events/{event_id}/registrations",
        "/events/{event_id}/publish",
    ):
        assert NOT_VERIFIED in paths[path]["post"]["responses"]["403"]["description"]
