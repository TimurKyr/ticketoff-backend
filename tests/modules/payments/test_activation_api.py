"""Activation API (BE-14).

The API under test commits through its own sessions, so (like tests/test_events.py) these
tests build rows with a committing session and give every test its own users and event.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token, hash_password
from app.modules.orders.enums import TicketKind
from app.modules.orders.models import TicketType
from app.modules.payments.models import Payment
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
STEPS = ("simulate-verification", "accept-terms", "pay")


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def _user(db: Session, organizer: bool = False) -> tuple[User, Headers]:
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash=hash_password("x" * 12))
    db.add(user)
    db.flush()
    if organizer:
        db.add(OrganizerProfile(user_id=user.id, display_name="Organizer"))
    db.commit()
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _event(
    db: Session, owner: User, status: EventStatus = EventStatus.PUBLISHED, paid_type: bool = True
) -> Event:
    profile = db.scalar(select(OrganizerProfile).where(OrganizerProfile.user_id == owner.id))
    venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}", city="Almaty")
    db.add(venue)
    db.flush()
    event = Event(
        organizer_id=profile.id,
        venue_id=venue.id,
        title="Paid concert",
        status=status,
        start_at=datetime.now(UTC) + timedelta(days=30),
    )
    db.add(event)
    db.flush()
    if paid_type:
        db.add(
            TicketType(
                event_id=event.id,
                name="Standard",
                kind=TicketKind.PAID,
                price_kzt=15000,
                quantity_total=100,
                max_per_order=10,
            )
        )
    db.commit()
    return event


@pytest.fixture
def owner(db: Session) -> tuple[User, Headers]:
    return _user(db, organizer=True)


def _url(event: Event, step: str = "") -> str:
    return f"/events/{event.id}/activation" + (f"/{step}" if step else "")


def test_get_checklist_shape(client: TestClient, db: Session, owner) -> None:
    event = _event(db, owner[0])

    response = client.get(_url(event), headers=owner[1])

    assert response.status_code == 200
    assert response.json() == {
        "has_paid_ticket_type": True,
        "identity_verified": False,
        "payout_account_ready": False,
        "activation_fee_paid": False,
        "terms_accepted": False,
        "active": False,
        "activation_fee_kzt": get_settings().activation_fee_kzt,
        "is_simulated": True,
    }


def test_full_flow_activates_the_event(client: TestClient, db: Session, owner) -> None:
    event = _event(db, owner[0])

    bodies = [client.post(_url(event, step), headers=owner[1]) for step in STEPS]

    assert [r.status_code for r in bodies] == [200, 200, 200]
    assert [r.json()["active"] for r in bodies] == [False, False, True]
    assert all(r.json()["is_simulated"] is True for r in bodies)
    assert client.get(_url(event), headers=owner[1]).json()["active"] is True
    db.expire_all()
    assert db.get(Event, event.id).paid_sales_active is True


def test_pay_twice_over_http_creates_one_payment(client: TestClient, db: Session, owner) -> None:
    event = _event(db, owner[0])

    for _ in range(2):
        assert client.post(_url(event, "pay"), headers=owner[1]).status_code == 200

    assert len(db.scalars(select(Payment).where(Payment.event_id == event.id)).all()) == 1


@pytest.mark.parametrize("step", ["", *STEPS])
def test_other_organizer_gets_404(client: TestClient, db: Session, owner, step: str) -> None:
    event = _event(db, owner[0])
    _, stranger = _user(db, organizer=True)

    call = client.get if not step else client.post
    response = call(_url(event, step), headers=stranger)

    assert response.status_code == 404


def test_non_organizer_gets_403_and_anonymous_401(client: TestClient, db: Session, owner) -> None:
    event = _event(db, owner[0])
    _, attendee = _user(db)

    assert client.post(_url(event, "pay"), headers=attendee).status_code == 403
    assert client.post(_url(event, "pay")).status_code == 401


def test_unknown_event_gets_404(client: TestClient, owner) -> None:
    response = client.get(f"/events/{uuid.uuid4()}/activation", headers=owner[1])

    assert response.status_code == 404


@pytest.mark.parametrize("step", STEPS)
def test_cancelled_event_gets_409(client: TestClient, db: Session, owner, step: str) -> None:
    event = _event(db, owner[0], status=EventStatus.CANCELLED)

    response = client.post(_url(event, step), headers=owner[1])

    assert response.status_code == 409
    # The checklist can still be read.
    assert client.get(_url(event), headers=owner[1]).status_code == 200


def test_draft_event_can_be_activated(client: TestClient, db: Session, owner) -> None:
    event = _event(db, owner[0], status=EventStatus.DRAFT)

    responses = [client.post(_url(event, step), headers=owner[1]) for step in STEPS]

    assert responses[-1].status_code == 200
    assert responses[-1].json()["active"] is True
