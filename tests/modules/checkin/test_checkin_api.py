"""Check-in API (BE-15).

The API under test commits through its own sessions, so (like tests/test_events.py) these
tests build rows with a committing session and give every test its own users and event.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, engine
from app.modules.audit.models import AuditLog
from app.modules.checkin import repository as checkin_repository
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, StaffAssignment, User
from app.modules.identity.security import create_access_token, hash_password
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import OrderStatus, TicketKind, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.qr import issue_ticket_token

Headers = dict[str, str]

postgres_only = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="needs row locks and partial unique indexes (Postgres)",
)


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


@dataclass
class Door:
    """One published event with its owner, an assigned staff member and an outsider."""

    event: Event
    owner: Headers
    staff: Headers
    staff_user: User
    outsider: Headers
    add_ticket: Callable[..., Ticket] = field(repr=False)


def _make_event(db: Session, organizer_user: User) -> Event:
    profile = db.scalar(
        select(OrganizerProfile).where(OrganizerProfile.user_id == organizer_user.id)
    )
    venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}", city="Almaty")
    db.add(venue)
    db.flush()
    event = Event(
        organizer_id=profile.id,
        venue_id=venue.id,
        title="Door test",
        status=EventStatus.PUBLISHED,
        start_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(event)
    db.commit()
    return event


def _ticket_factory(db: Session, event: Event) -> Callable[..., Ticket]:
    ticket_type = TicketType(
        event_id=event.id, name="General", kind=TicketKind.FREE, price_kzt=0, quantity_total=100
    )
    db.add(ticket_type)
    db.commit()

    def add(name: str = "Aigerim", status: TicketStatus = TicketStatus.VALID) -> Ticket:
        buyer = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(buyer)
        db.flush()
        order = Order(buyer_user_id=buyer.id, event_id=event.id, status=OrderStatus.FREE_CONFIRMED)
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id, ticket_type_id=ticket_type.id, quantity=1, unit_price_kzt=0
        )
        attendee = Attendee(order_id=order.id, name=name, email=f"{name.lower()}@example.com")
        db.add_all([item, attendee])
        db.flush()
        ticket_id = uuid.uuid4()
        ticket = Ticket(
            id=ticket_id,
            order_item_id=item.id,
            attendee_id=attendee.id,
            qr_token=issue_ticket_token(ticket_id),
            status=status,
        )
        db.add(ticket)
        db.commit()
        return ticket

    return add


@pytest.fixture
def door(db: Session) -> Door:
    owner_user, owner = _user(db, organizer=True)
    staff_user, staff = _user(db)
    _, outsider = _user(db)
    event = _make_event(db, owner_user)
    db.add(StaffAssignment(event_id=event.id, user_id=staff_user.id))
    db.commit()
    return Door(event, owner, staff, staff_user, outsider, _ticket_factory(db, event))


def _scan(client: TestClient, door: Door, token: str, headers: Headers | None = None) -> dict:
    response = client.post(
        "/checkin/scan",
        json={"event_id": str(door.event.id), "qr_token": token},
        headers=headers or door.staff,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- scan results -----------------------------------------------------------------------------


def test_valid_ticket_is_checked_in_once(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket(name="Aigerim")
    door.add_ticket()  # a second guest who has not arrived

    first = _scan(client, door, ticket.qr_token)
    second = _scan(client, door, ticket.qr_token)

    assert first["result"] == "checked_in"
    assert first["ticket"]["attendee_name"] == "Aigerim"
    assert first["ticket"]["ticket_type"] == "General"
    assert (first["checked_in_count"], first["total_count"]) == (1, 2)
    assert second["result"] == "already_used"
    assert second["checked_in_count"] == 1


def test_check_in_writes_a_record(client: TestClient, door: Door, db: Session) -> None:
    ticket = door.add_ticket()

    _scan(client, door, ticket.qr_token)

    records = db.scalars(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)).all()
    assert len(records) == 1
    assert records[0].event_admin_id == door.staff_user.id
    assert records[0].is_reversed is False


@pytest.mark.parametrize(
    "token",
    ["tk1.00000000-0000-0000-0000-000000000000.forged", "garbage", "eyJhbGciOiJIUzI1NiJ9.x.y"],
)
def test_unknown_or_foreign_tokens_are_invalid(client: TestClient, door: Door, token: str) -> None:
    assert _scan(client, door, token)["result"] == "invalid"


def test_forged_signature_is_invalid_without_db_lookup(
    client: TestClient, door: Door, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = door.add_ticket()
    forged = f"{ticket.qr_token.rpartition('.')[0]}.forged-signature"

    def no_lookup(*args: object) -> None:
        raise AssertionError("forged tokens must be rejected before the DB lookup")

    monkeypatch.setattr(checkin_repository, "find_ticket_for_update", no_lookup)

    assert _scan(client, door, forged)["result"] == "invalid"


def test_signed_token_of_unknown_ticket_is_invalid(client: TestClient, door: Door) -> None:
    assert _scan(client, door, issue_ticket_token(uuid.uuid4()))["result"] == "invalid"


def test_campaign_qr_is_rejected(client: TestClient, door: Door) -> None:
    result = _scan(client, door, f"pc1.{uuid.uuid4()}.sig")

    assert result["result"] == "campaign_qr_rejected"
    assert result["ticket"] is None


@pytest.mark.parametrize(
    ("status", "result"),
    [(TicketStatus.CANCELLED, "cancelled"), (TicketStatus.REFUNDED, "refunded")],
)
def test_cancelled_and_refunded_tickets_are_refused(
    client: TestClient, door: Door, status: TicketStatus, result: str
) -> None:
    ticket = door.add_ticket(status=status)

    response = _scan(client, door, ticket.qr_token)

    assert response["result"] == result
    assert response["total_count"] == 0  # refused tickets are not "registered"


def test_ticket_of_another_event_is_wrong_event(
    client: TestClient, door: Door, db: Session
) -> None:
    owner_user, _ = _user(db, organizer=True)
    other_ticket = _ticket_factory(db, _make_event(db, owner_user))()

    assert _scan(client, door, other_ticket.qr_token)["result"] == "wrong_event"


def test_token_whitespace_is_ignored(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    assert _scan(client, door, f"  {ticket.qr_token}\n")["result"] == "checked_in"


def test_scan_answers_fast(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    started = time.perf_counter()
    _scan(client, door, ticket.qr_token)

    assert time.perf_counter() - started < 2  # spec: QR check under 2 seconds


# --- who may scan ------------------------------------------------------------------------------


def test_owner_may_scan_without_assignment(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    assert _scan(client, door, ticket.qr_token, headers=door.owner)["result"] == "checked_in"


def test_unassigned_user_gets_403(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    response = client.post(
        "/checkin/scan",
        json={"event_id": str(door.event.id), "qr_token": ticket.qr_token},
        headers=door.outsider,
    )

    assert response.status_code == 403
    assert (
        client.get(f"/checkin/events/{door.event.id}/stats", headers=door.outsider).status_code
        == 403
    )


def test_unknown_event_is_404(client: TestClient, door: Door) -> None:
    response = client.post(
        "/checkin/scan",
        json={"event_id": str(uuid.uuid4()), "qr_token": "tk1.x"},
        headers=door.staff,
    )

    assert response.status_code == 404


def test_scan_requires_login(client: TestClient, door: Door) -> None:
    response = client.post("/checkin/scan", json={"event_id": str(door.event.id), "qr_token": "x"})

    assert response.status_code == 401


# --- undo ---------------------------------------------------------------------------------------


def test_undo_reverses_check_in_and_is_audited(client: TestClient, door: Door, db: Session) -> None:
    ticket = door.add_ticket()
    _scan(client, door, ticket.qr_token)

    response = client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.staff)

    assert response.status_code == 200
    assert response.json()["status"] == "valid"
    record = db.scalar(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id))
    assert record.is_reversed is True
    audit = db.scalar(select(AuditLog).where(AuditLog.entity_id == ticket.id))
    assert audit.action_type == "ticket.check_in_reversed"
    assert audit.event_id == door.event.id


def test_undo_of_a_ticket_that_is_not_checked_in_is_409(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    assert client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.staff).status_code == 409


def test_undo_by_unassigned_user_is_403(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    _scan(client, door, ticket.qr_token)

    response = client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.outsider)

    assert response.status_code == 403


@postgres_only
def test_after_undo_the_ticket_can_be_checked_in_again(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    _scan(client, door, ticket.qr_token)
    client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.staff)

    assert _scan(client, door, ticket.qr_token)["result"] == "checked_in"


@postgres_only
def test_two_simultaneous_scans_admit_once(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def scan_once() -> None:
        barrier.wait()
        results.append(_scan(client, door, ticket.qr_token)["result"])

    threads = [threading.Thread(target=scan_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["already_used", "checked_in"]


# --- staff management -----------------------------------------------------------------------


def test_owner_manages_staff(client: TestClient, door: Door, db: Session) -> None:
    new_user, new_headers = _user(db)
    url = f"/events/{door.event.id}/staff"

    created = client.post(url, json={"email": new_user.email.upper()}, headers=door.owner)
    duplicate = client.post(url, json={"email": new_user.email}, headers=door.owner)
    listed = client.get(url, headers=door.owner).json()

    assert created.status_code == 201
    assert created.json()["role"] == "event_admin"
    assert duplicate.status_code == 409
    assert {row["email"] for row in listed} == {new_user.email, door.staff_user.email}
    assert [e["id"] for e in client.get("/checkin/my-events", headers=new_headers).json()] == [
        str(door.event.id)
    ]

    assert client.delete(f"{url}/{new_user.id}", headers=door.owner).status_code == 204
    assert client.get("/checkin/my-events", headers=new_headers).json() == []
    assert client.delete(f"{url}/{new_user.id}", headers=door.owner).status_code == 404


def test_unknown_email_is_404(client: TestClient, door: Door) -> None:
    response = client.post(
        f"/events/{door.event.id}/staff", json={"email": "nobody@example.com"}, headers=door.owner
    )

    assert response.status_code == 404


def test_only_the_owner_manages_staff(client: TestClient, door: Door, db: Session) -> None:
    _, other_organizer = _user(db, organizer=True)
    url = f"/events/{door.event.id}/staff"

    for headers in (other_organizer, door.staff):
        assert client.get(url, headers=headers).status_code == 404
        assert client.post(url, json={"email": "x@example.com"}, headers=headers).status_code == 404


def test_my_events_marks_owned_events(client: TestClient, door: Door) -> None:
    owned = client.get("/checkin/my-events", headers=door.owner).json()
    staffed = client.get("/checkin/my-events", headers=door.staff).json()

    assert [(e["id"], e["is_owner"]) for e in owned] == [(str(door.event.id), True)]
    assert [(e["id"], e["is_owner"]) for e in staffed] == [(str(door.event.id), False)]
    assert client.get("/checkin/my-events", headers=door.outsider).json() == []


# --- door-side lists ----------------------------------------------------------------------------


def test_attendee_search_and_stats(client: TestClient, door: Door) -> None:
    arrived = door.add_ticket(name="Aigerim")
    door.add_ticket(name="Bolat")
    door.add_ticket(name="Dana", status=TicketStatus.REFUNDED)
    _scan(client, door, arrived.qr_token)
    base = f"/checkin/events/{door.event.id}"

    found = client.get(f"{base}/attendees", params={"q": "aig"}, headers=door.staff).json()
    everyone = client.get(f"{base}/attendees", headers=door.staff).json()
    stats = client.get(f"{base}/stats", headers=door.staff).json()

    assert [(r["attendee_name"], r["status"]) for r in found] == [("Aigerim", "checked_in")]
    assert [r["attendee_name"] for r in everyone] == ["Aigerim", "Bolat", "Dana"]
    assert stats == {"registered": 2, "checked_in": 1, "absent": 1, "percent": 50.0}


def test_attendee_search_treats_wildcards_literally(client: TestClient, door: Door) -> None:
    door.add_ticket(name="Aigerim")

    response = client.get(
        f"/checkin/events/{door.event.id}/attendees", params={"q": "%"}, headers=door.staff
    )

    assert response.json() == []


def test_stats_of_an_empty_event(client: TestClient, door: Door) -> None:
    stats = client.get(f"/checkin/events/{door.event.id}/stats", headers=door.owner).json()

    assert stats == {"registered": 0, "checked_in": 0, "absent": 0, "percent": 0.0}
