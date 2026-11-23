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


def test_unassigned_user_gets_404_like_a_missing_event(client: TestClient, door: Door) -> None:
    """Someone else's event is not revealed: same 404 as an unknown id."""
    ticket = door.add_ticket()
    base = f"/checkin/events/{door.event.id}"

    response = client.post(
        "/checkin/scan",
        json={"event_id": str(door.event.id), "qr_token": ticket.qr_token},
        headers=door.outsider,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
    assert client.get(f"{base}/stats", headers=door.outsider).status_code == 404
    assert client.get(f"{base}/attendees", headers=door.outsider).status_code == 404


def test_staff_of_another_event_gets_404(client: TestClient, door: Door, db: Session) -> None:
    owner_user, _ = _user(db, organizer=True)
    other_event = _make_event(db, owner_user)

    response = client.post(
        "/checkin/scan",
        json={"event_id": str(other_event.id), "qr_token": "tk1.x"},
        headers=door.staff,
    )

    assert response.status_code == 404


def test_unknown_event_is_404(client: TestClient, door: Door) -> None:
    response = client.post(
        "/checkin/scan",
        json={"event_id": str(uuid.uuid4()), "qr_token": "tk1.x"},
        headers=door.staff,
    )

    assert response.status_code == 404


def _set_status(db: Session, event: Event, status: EventStatus) -> None:
    db.get(Event, event.id).status = status
    db.commit()


def test_scan_on_a_cancelled_event_admits_nobody(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    _set_status(db, door.event, EventStatus.CANCELLED)

    body = _scan(client, door, ticket.qr_token)

    assert body["result"] == "event_cancelled"
    assert body["ticket"] is None
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == TicketStatus.VALID


def test_scan_on_a_draft_event_is_invalid(client: TestClient, door: Door, db: Session) -> None:
    ticket = door.add_ticket()
    _set_status(db, door.event, EventStatus.DRAFT)

    assert _scan(client, door, ticket.qr_token)["result"] == "invalid"
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == TicketStatus.VALID


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
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == ticket.id, AuditLog.action_type == "ticket.check_in_reversed"
        )
    )
    assert audit.action_type == "ticket.check_in_reversed"
    assert audit.event_id == door.event.id


def test_undo_of_a_ticket_that_is_not_checked_in_is_409(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    assert client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.staff).status_code == 409


def test_undo_of_a_foreign_ticket_looks_like_an_unknown_one(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    _scan(client, door, ticket.qr_token)

    foreign = client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.outsider)
    unknown = client.post(f"/checkin/tickets/{uuid.uuid4()}/undo", headers=door.outsider)

    assert (foreign.status_code, foreign.json()) == (404, {"detail": "Ticket not found"})
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Ticket not found"})
    # The outsider changed nothing: the ticket is still checked in.
    assert _scan(client, door, ticket.qr_token)["result"] == "already_used"


@postgres_only
def test_undo_checks_access_before_locking_the_ticket(client: TestClient, door: Door) -> None:
    """An outsider's undo answers 404 at once, without waiting for the ticket row lock."""
    ticket = door.add_ticket()
    statuses: list[int] = []
    with SessionLocal() as holder:
        checkin_repository.get_ticket_for_update(holder, ticket.id)  # held until rollback
        thread = threading.Thread(
            target=lambda: statuses.append(
                client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.outsider).status_code
            )
        )
        thread.start()
        thread.join(timeout=2)
        answered_while_locked = not thread.is_alive()
        holder.rollback()
    thread.join(timeout=10)

    assert answered_while_locked, "undo waited for the lock before checking access"
    assert statuses == [404]


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


# --- manual check-in by ticket id ---------------------------------------------------------------


def _check_in(client: TestClient, ticket_id: uuid.UUID, headers: Headers) -> dict:
    response = client.post(f"/checkin/tickets/{ticket_id}/check-in", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_check_in_admits_once_and_is_audited(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket(name="Aigerim")
    door.add_ticket()  # a second guest who has not arrived

    first = _check_in(client, ticket.id, door.staff)
    second = _check_in(client, ticket.id, door.staff)

    assert first["result"] == "checked_in"
    assert first["ticket"]["attendee_name"] == "Aigerim"
    assert first["ticket"]["status"] == "checked_in"
    assert (first["checked_in_count"], first["total_count"]) == (1, 2)
    assert second["result"] == "already_used"
    assert second["checked_in_count"] == 1
    # The QR of a guest admitted by hand is used too.
    assert _scan(client, door, ticket.qr_token)["result"] == "already_used"
    record = db.scalar(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id))
    assert record.event_admin_id == door.staff_user.id
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == ticket.id, AuditLog.action_type == "ticket.checked_in"
        )
    )
    assert audit.details["method"] == "manual"
    assert audit.event_id == door.event.id


def test_scan_is_audited_as_qr(client: TestClient, door: Door, db: Session) -> None:
    ticket = door.add_ticket()
    _scan(client, door, ticket.qr_token)

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == ticket.id, AuditLog.action_type == "ticket.checked_in"
        )
    )
    assert audit.details["method"] == "qr"


def test_owner_may_check_in_by_hand(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    assert _check_in(client, ticket.id, door.owner)["result"] == "checked_in"


def test_manual_check_in_of_a_foreign_or_unknown_ticket_is_404(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    # Staff of another event may not admit this event's guests.
    other_owner, _ = _user(db, organizer=True)
    other_event = _make_event(db, other_owner)
    db.add(StaffAssignment(event_id=other_event.id, user_id=door.staff_user.id))
    other_staff_user, other_staff = _user(db)
    db.add(StaffAssignment(event_id=other_event.id, user_id=other_staff_user.id))
    db.commit()

    for headers in (door.outsider, other_staff):
        foreign = client.post(f"/checkin/tickets/{ticket.id}/check-in", headers=headers)
        assert (foreign.status_code, foreign.json()) == (404, {"detail": "Ticket not found"})
    unknown = client.post(f"/checkin/tickets/{uuid.uuid4()}/check-in", headers=door.staff)
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Ticket not found"})
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == TicketStatus.VALID


def test_manual_check_in_requires_login(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    assert client.post(f"/checkin/tickets/{ticket.id}/check-in").status_code == 401


@pytest.mark.parametrize("status", [TicketStatus.CANCELLED, TicketStatus.REFUNDED])
def test_manual_check_in_refuses_cancelled_and_refunded_tickets(
    client: TestClient, door: Door, db: Session, status: TicketStatus
) -> None:
    ticket = door.add_ticket(status=status)

    body = _check_in(client, ticket.id, door.staff)

    assert body["result"] == status.value
    assert body["ticket"]["status"] == status.value
    assert (body["checked_in_count"], body["total_count"]) == (0, 0)
    assert db.scalar(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)) is None


def test_manual_check_in_on_a_cancelled_event_admits_nobody(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    _set_status(db, door.event, EventStatus.CANCELLED)

    body = _check_in(client, ticket.id, door.staff)

    assert body["result"] == "event_cancelled"
    assert body["ticket"] is None
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == TicketStatus.VALID


def test_manual_check_in_on_a_draft_event_is_invalid(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    _set_status(db, door.event, EventStatus.DRAFT)

    assert _check_in(client, ticket.id, door.staff)["result"] == "invalid"


@postgres_only
def test_undo_then_manual_check_in_admits_again(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    _check_in(client, ticket.id, door.staff)
    undo = client.post(f"/checkin/tickets/{ticket.id}/undo", headers=door.staff)
    assert undo.status_code == 200

    again = _check_in(client, ticket.id, door.staff)

    assert again["result"] == "checked_in"
    assert again["checked_in_count"] == 1
    records = db.scalars(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)).all()
    assert sorted(r.is_reversed for r in records) == [False, True]


@postgres_only
def test_manual_check_in_checks_access_before_locking_the_ticket(
    client: TestClient, door: Door
) -> None:
    ticket = door.add_ticket()
    statuses: list[int] = []
    with SessionLocal() as holder:
        checkin_repository.get_ticket_for_update(holder, ticket.id)  # held until rollback
        thread = threading.Thread(
            target=lambda: statuses.append(
                client.post(
                    f"/checkin/tickets/{ticket.id}/check-in", headers=door.outsider
                ).status_code
            )
        )
        thread.start()
        thread.join(timeout=2)
        answered_while_locked = not thread.is_alive()
        holder.rollback()
    thread.join(timeout=10)

    assert answered_while_locked, "check-in waited for the lock before checking access"
    assert statuses == [404]


@postgres_only
def test_two_simultaneous_manual_check_ins_admit_once(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def check_in_once(headers: Headers) -> None:
        barrier.wait()
        results.append(_check_in(client, ticket.id, headers)["result"])

    threads = [threading.Thread(target=check_in_once, args=(h,)) for h in (door.staff, door.owner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["already_used", "checked_in"]
    assert (
        len(db.scalars(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)).all())
        == 1
    )


@postgres_only
def test_manual_check_in_racing_a_scan_admits_once(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def by_hand() -> None:
        barrier.wait()
        results.append(_check_in(client, ticket.id, door.owner)["result"])

    def by_qr() -> None:
        barrier.wait()
        results.append(_scan(client, door, ticket.qr_token)["result"])

    threads = [threading.Thread(target=by_hand), threading.Thread(target=by_qr)]
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


def test_my_events_include_the_venue(client: TestClient, door: Door, db: Session) -> None:
    venue = db.get(Venue, door.event.venue_id)
    venue.address = "Abay Ave 1"
    venue.timezone = "Asia/Aqtobe"
    db.commit()

    (event,) = client.get("/checkin/my-events", headers=door.staff).json()

    assert (event["venue_name"], event["venue_address"], event["timezone"]) == (
        venue.name,
        "Abay Ave 1",
        "Asia/Aqtobe",
    )


def test_me_lists_staff_events_once_in_a_stable_order(
    client: TestClient, door: Door, db: Session
) -> None:
    owner_user, _ = _user(db, organizer=True)
    other_event = _make_event(db, owner_user)
    # No unique constraint on (event, user) yet, so a duplicate row can exist.
    db.add_all(
        [
            StaffAssignment(event_id=other_event.id, user_id=door.staff_user.id),
            StaffAssignment(event_id=door.event.id, user_id=door.staff_user.id),
        ]
    )
    db.commit()

    ids = client.get("/auth/me", headers=door.staff).json()["staff_event_ids"]

    assert ids == sorted({str(door.event.id), str(other_event.id)}, key=uuid.UUID)


# --- door-side lists ----------------------------------------------------------------------------


def test_attendee_search_and_stats(client: TestClient, door: Door) -> None:
    arrived = door.add_ticket(name="Aigerim")
    door.add_ticket(name="Bolat")
    door.add_ticket(name="Dana", status=TicketStatus.REFUNDED)
    _scan(client, door, arrived.qr_token)
    base = f"/checkin/events/{door.event.id}"

    found = client.get(f"{base}/attendees", params={"q": "aig"}, headers=door.staff).json()["items"]
    everyone = client.get(f"{base}/attendees", headers=door.staff).json()["items"]
    stats = client.get(f"{base}/stats", headers=door.staff).json()

    assert [(r["attendee_name"], r["status"]) for r in found] == [("Aigerim", "checked_in")]
    assert [r["attendee_name"] for r in everyone] == ["Aigerim", "Bolat", "Dana"]
    assert stats == {"registered": 2, "checked_in": 1, "absent": 1, "percent": 50.0}


def test_attendee_search_treats_wildcards_literally(client: TestClient, door: Door) -> None:
    door.add_ticket(name="Aigerim")

    response = client.get(
        f"/checkin/events/{door.event.id}/attendees", params={"q": "%"}, headers=door.staff
    )

    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_attendees_are_paged(client: TestClient, door: Door) -> None:
    for name in ("Aigerim", "Bolat", "Dana", "Erlan", "Farida"):
        door.add_ticket(name=name)
    url = f"/checkin/events/{door.event.id}/attendees"

    page = client.get(url, params={"limit": 2, "offset": 2}, headers=door.staff).json()

    assert [r["attendee_name"] for r in page["items"]] == ["Dana", "Erlan"]
    assert (page["total"], page["limit"], page["offset"]) == (5, 2, 2)
    last = client.get(url, params={"limit": 2, "offset": 4}, headers=door.staff).json()
    assert [r["attendee_name"] for r in last["items"]] == ["Farida"]
    filtered = client.get(url, params={"q": "da", "limit": 1}, headers=door.staff).json()
    assert filtered["total"] == 2  # Dana, Farida
    assert client.get(url, params={"offset": -1}, headers=door.staff).status_code == 422


def test_stats_of_an_empty_event(client: TestClient, door: Door) -> None:
    stats = client.get(f"/checkin/events/{door.event.id}/stats", headers=door.owner).json()

    assert stats == {"registered": 0, "checked_in": 0, "absent": 0, "percent": 0.0}


# --- offline pack and sync (BE-32) --------------------------------------------------------------


def _pack(client: TestClient, door: Door, headers: Headers | None = None) -> dict:
    response = client.get(
        f"/checkin/events/{door.event.id}/offline-pack", headers=headers or door.staff
    )
    assert response.status_code == 200, response.text
    return response.json()


def _item(ticket_id: object, device: str = "phone-a", scanned_at: datetime | None = None) -> dict:
    when = scanned_at or datetime.now(UTC) - timedelta(minutes=5)
    return {"ticket_id": str(ticket_id), "scanned_at": when.isoformat(), "device_id": device}


def _sync(client: TestClient, door: Door, items: list[dict], headers: Headers | None = None):
    response = client.post(
        "/checkin/sync",
        json={"event_id": str(door.event.id), "items": items},
        headers=headers or door.staff,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_offline_pack_lists_every_ticket_without_qr_secrets(
    client: TestClient, door: Door, db: Session
) -> None:
    valid = door.add_ticket("Aigerim")
    cancelled = door.add_ticket("Bolat", status=TicketStatus.CANCELLED)
    before = datetime.now(UTC)

    body = _pack(client, door)

    assert body["event_id"] == str(door.event.id)
    assert body["event_status"] == "published"
    assert datetime.fromisoformat(body["generated_at"]) >= before - timedelta(seconds=1)
    assert body["tickets"] == [
        {"ticket_id": str(valid.id), "status": "valid", "attendee_name": "Aigerim",
         "ticket_type": "General"},
        {"ticket_id": str(cancelled.id), "status": "cancelled", "attendee_name": "Bolat",
         "ticket_type": "General"},
    ]  # fmt: skip
    assert (body["checked_in_count"], body["total_count"]) == (0, 1)
    assert "tk1." not in str(body) and "qr_token" not in str(body)
    entry = db.scalar(
        select(AuditLog).where(
            AuditLog.event_id == door.event.id,
            AuditLog.action_type == "event.offline_pack_downloaded",
        )
    )
    assert entry is not None and entry.details == {"tickets": 2}


def test_offline_pack_is_for_the_owner_and_assigned_staff_only(
    client: TestClient, door: Door, db: Session
) -> None:
    door.add_ticket()
    assert len(_pack(client, door, door.owner)["tickets"]) == 1

    url = f"/checkin/events/{door.event.id}/offline-pack"
    assert client.get(url, headers=door.outsider).status_code == 404
    assert client.get(url).status_code == 401
    owner_user, _ = _user(db, organizer=True)
    other_event = _make_event(db, owner_user)
    response = client.get(f"/checkin/events/{other_event.id}/offline-pack", headers=door.staff)
    assert response.status_code == 404


def test_sync_of_one_ticket_from_two_devices_admits_once(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    first_scan = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)

    first = _sync(client, door, [_item(ticket.id, "phone-a", first_scan)])
    second = _sync(client, door, [_item(ticket.id, "phone-b")], headers=door.owner)

    assert first["items"][0]["result"] == "checked_in"
    assert first["items"][0]["ticket"]["status"] == "checked_in"
    assert (first["checked_in_count"], first["total_count"]) == (1, 1)
    conflict = second["items"][0]
    assert conflict["result"] == "already_used"
    assert conflict["device_id"] == "phone-b"
    assert conflict["checked_in_by"] == door.staff_user.email
    assert conflict["checked_in_device_id"] == "phone-a"
    assert datetime.fromisoformat(conflict["checked_in_at"]) == first_scan
    records = db.scalars(select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)).all()
    assert len(records) == 1 and records[0].checked_in_at == first_scan
    entry = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == ticket.id, AuditLog.action_type == "ticket.checked_in"
        )
    )
    assert entry.details["method"] == "offline"
    assert entry.details["device_id"] == "phone-a"


def test_resending_a_sync_is_safe(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    items = [_item(ticket.id, "phone-a")]

    assert _sync(client, door, items)["items"][0]["result"] == "checked_in"
    again = _sync(client, door, items)

    assert again["items"][0]["result"] == "already_used"
    assert again["items"][0]["checked_in_device_id"] == "phone-a"
    assert again["checked_in_count"] == 1


def test_ticket_cancelled_after_the_pack_is_refused_at_sync(
    client: TestClient, door: Door, db: Session
) -> None:
    cancelled = door.add_ticket("Aigerim")
    refunded = door.add_ticket("Bolat")
    assert {t["status"] for t in _pack(client, door)["tickets"]} == {"valid"}
    db.get(Ticket, cancelled.id).status = TicketStatus.CANCELLED
    db.get(Ticket, refunded.id).status = TicketStatus.REFUNDED
    db.commit()

    body = _sync(client, door, [_item(cancelled.id), _item(refunded.id)])

    assert [i["result"] for i in body["items"]] == ["cancelled", "refunded"]
    assert body["items"][0]["checked_in_at"] is None
    db.expire_all()
    assert db.get(Ticket, cancelled.id).status == TicketStatus.CANCELLED
    assert db.scalar(select(CheckInRecord).where(CheckInRecord.ticket_id == cancelled.id)) is None


def test_sync_answers_in_sent_order_and_the_earliest_scan_wins(
    client: TestClient, door: Door
) -> None:
    ticket = door.add_ticket()
    early = datetime.now(UTC) - timedelta(hours=1)
    late = datetime.now(UTC) - timedelta(minutes=1)

    body = _sync(client, door, [_item(ticket.id, "late", late), _item(ticket.id, "early", early)])

    assert [i["result"] for i in body["items"]] == ["already_used", "checked_in"]
    assert body["items"][0]["checked_in_device_id"] == "early"


def test_scan_time_in_the_future_is_recorded_as_now(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    before = datetime.now(UTC)

    body = _sync(client, door, [_item(ticket.id, scanned_at=before + timedelta(days=1))])

    assert body["items"][0]["result"] == "checked_in"
    checked_in_at = datetime.fromisoformat(body["items"][0]["checked_in_at"])
    assert before <= checked_in_at <= datetime.now(UTC)


def test_sync_rejects_campaign_qr_and_foreign_or_unknown_tickets(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    owner_user, _ = _user(db, organizer=True)
    foreign = _ticket_factory(db, _make_event(db, owner_user))()

    body = _sync(
        client,
        door,
        [
            _item("pc1.abcdef"),
            _item("https://ticketoff.store/e/x?promo=pc1.abcdef"),
            _item(uuid.uuid4()),
            _item("not-a-ticket"),
            _item("{" + str(ticket.id) + "}"),
            _item(ticket.qr_token[:-2] + "xx"),  # forged signature
            _item(foreign.id),
        ],
    )

    assert [i["result"] for i in body["items"]] == [
        "campaign_qr_rejected",
        "campaign_qr_rejected",
        "invalid",
        "invalid",
        "invalid",
        "invalid",
        "wrong_event",
    ]
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == TicketStatus.VALID
    assert db.get(Ticket, foreign.id).status == TicketStatus.VALID


def test_sync_accepts_the_whole_ticket_qr(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()

    body = _sync(client, door, [_item(ticket.qr_token)])

    assert body["items"][0]["result"] == "checked_in"
    assert body["items"][0]["ticket_id"] == ticket.qr_token  # echoed as sent
    assert body["items"][0]["ticket"]["id"] == str(ticket.id)


def test_sync_on_a_cancelled_event_admits_nobody(
    client: TestClient, door: Door, db: Session
) -> None:
    ticket = door.add_ticket()
    _set_status(db, door.event, EventStatus.CANCELLED)

    assert _sync(client, door, [_item(ticket.id)])["items"][0]["result"] == "event_cancelled"
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == TicketStatus.VALID


def test_sync_access_and_validation(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    payload = {"event_id": str(door.event.id), "items": [_item(ticket.id)]}

    assert client.post("/checkin/sync", json=payload, headers=door.outsider).status_code == 404
    assert client.post("/checkin/sync", json=payload).status_code == 401
    naive = {**payload, "items": [{**_item(ticket.id), "scanned_at": "2026-11-26T14:05:00"}]}
    assert client.post("/checkin/sync", json=naive, headers=door.staff).status_code == 422
    empty = {**payload, "items": []}
    assert client.post("/checkin/sync", json=empty, headers=door.staff).status_code == 422


@postgres_only
def test_two_devices_syncing_at_once_admit_once(client: TestClient, door: Door) -> None:
    ticket = door.add_ticket()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def sync_once(device: str, headers: Headers) -> None:
        barrier.wait()
        body = _sync(client, door, [_item(ticket.id, device)], headers)
        results.append(body["items"][0]["result"])

    threads = [
        threading.Thread(target=sync_once, args=("phone-a", door.staff)),
        threading.Thread(target=sync_once, args=("phone-b", door.owner)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["already_used", "checked_in"]
