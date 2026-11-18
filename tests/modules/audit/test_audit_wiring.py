"""Audit rows for publish/unpublish, capacity, ticket-type price/quantity and check-in
(BE-19 part 2): one row per action, in the action's own transaction."""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_session
from app.main import app
from app.modules.audit.models import AuditLog
from app.modules.checkin import repository as checkin_repository
from app.modules.checkin import service as checkin_service
from app.modules.checkin.schemas import ScanResult
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.models import CheckInRecord
from app.modules.orders import service as orders_service
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import RegistrationCreate, TicketOut
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


def _headers(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _rows(session: Session, event_id: uuid.UUID, action_type: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.event_id == event_id)
    if action_type is not None:
        stmt = stmt.where(AuditLog.action_type == action_type)
    return list(session.scalars(stmt))


def _register(session: Session, buyer: User, ticket_type: TicketType) -> TicketOut:
    """One free ticket for `buyer` (committed through registration)."""
    data = RegistrationCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "attendees": [{"name": "Guest"}],
        }
    )
    return orders_service.register_free(session, buyer, ticket_type.event_id, data).tickets[0]


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def owner(db_session: Session, organizer: OrganizerProfile) -> User:
    return db_session.get_one(User, organizer.user_id)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    """Published, capacity 100."""
    event = create_event(db_session, organizer)
    event.capacity = 100
    db_session.flush()
    return event


@pytest.fixture
def ticket_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, name="VIP", price_kzt=5000, quantity_total=50)


# --- publish / unpublish ---


def test_publish_and_unpublish_are_audited(
    api: TestClient, db_session: Session, owner: User, organizer: OrganizerProfile
) -> None:
    draft = create_event(db_session, organizer, status=EventStatus.DRAFT)
    create_free_ticket_type(db_session, draft)

    assert api.post(f"/events/{draft.id}/publish", headers=_headers(owner)).status_code == 200
    assert api.post(f"/events/{draft.id}/unpublish", headers=_headers(owner)).status_code == 200

    published = _rows(db_session, draft.id, "event.published")
    unpublished = _rows(db_session, draft.id, "event.unpublished")
    for [row] in (published, unpublished):
        assert (row.entity_type, row.entity_id, row.actor_user_id, row.details) == (
            "event",
            draft.id,
            owner.id,
            None,
        )


def test_refused_publish_writes_nothing(
    api: TestClient, db_session: Session, owner: User, event: Event
) -> None:
    response = api.post(f"/events/{event.id}/publish", headers=_headers(owner))  # already published

    assert response.status_code == 409
    assert _rows(db_session, event.id) == []


# --- capacity ---


def _patch_event(api: TestClient, owner: User, event: Event, body: dict) -> None:
    response = api.patch(f"/events/{event.id}", json=body, headers=_headers(owner))
    assert response.status_code == 200, response.text


def test_capacity_change_is_audited_with_old_and_new(
    api: TestClient, db_session: Session, owner: User, event: Event
) -> None:
    _patch_event(api, owner, event, {"capacity": 80})

    [row] = _rows(db_session, event.id, "event.capacity_changed")
    assert (row.entity_type, row.entity_id, row.actor_user_id) == ("event", event.id, owner.id)
    assert row.details == {"old": 100, "new": 80}


@pytest.mark.parametrize(
    "body", [{"capacity": 100}, {"title": "Renamed"}, {}], ids=["same", "other", "empty"]
)
def test_no_op_or_unrelated_event_patch_writes_nothing(
    api: TestClient, db_session: Session, owner: User, event: Event, body: dict
) -> None:
    _patch_event(api, owner, event, body)

    assert _rows(db_session, event.id) == []


def test_capacity_between_null_and_a_number(
    api: TestClient, db_session: Session, owner: User, event: Event
) -> None:
    _patch_event(api, owner, event, {"capacity": None})  # 100 -> no limit
    _patch_event(api, owner, event, {"capacity": 60})  # no limit -> 60

    details = sorted(
        (r.details for r in _rows(db_session, event.id, "event.capacity_changed")),
        key=lambda d: d["old"] is None,
    )
    assert details == [{"old": 100, "new": None}, {"old": None, "new": 60}]


# --- ticket types ---


def _patch_type(api: TestClient, owner: User, ticket_type: TicketType, body: dict) -> None:
    url = f"/events/{ticket_type.event_id}/ticket-types/{ticket_type.id}"
    response = api.patch(url, json=body, headers=_headers(owner))
    assert response.status_code == 200, response.text


def test_price_change_is_audited(
    api: TestClient, db_session: Session, owner: User, event: Event, ticket_type: TicketType
) -> None:
    _patch_type(api, owner, ticket_type, {"price_kzt": 6000})

    [row] = _rows(db_session, event.id)
    assert (row.action_type, row.entity_type, row.entity_id, row.actor_user_id) == (
        "ticket_type.price_changed",
        "ticket_type",
        ticket_type.id,
        owner.id,
    )
    assert row.details == {"name": "VIP", "old": 5000, "new": 6000}


def test_quantity_change_is_audited(
    api: TestClient, db_session: Session, owner: User, event: Event, ticket_type: TicketType
) -> None:
    _patch_type(api, owner, ticket_type, {"quantity_total": 60})

    [row] = _rows(db_session, event.id)
    assert (row.action_type, row.details) == (
        "ticket_type.quantity_changed",
        {"name": "VIP", "old": 50, "new": 60},
    )


def test_price_and_quantity_together_write_two_rows(
    api: TestClient, db_session: Session, owner: User, event: Event, ticket_type: TicketType
) -> None:
    _patch_type(api, owner, ticket_type, {"price_kzt": 7000, "quantity_total": 40})

    by_action = {r.action_type: r.details for r in _rows(db_session, event.id)}
    assert by_action == {
        "ticket_type.price_changed": {"name": "VIP", "old": 5000, "new": 7000},
        "ticket_type.quantity_changed": {"name": "VIP", "old": 50, "new": 40},
    }


@pytest.mark.parametrize(
    "body",
    [{"price_kzt": 5000, "quantity_total": 50}, {"name": "Renamed"}, {"is_hidden": True}],
    ids=["same values", "name", "hidden"],
)
def test_no_op_or_unrelated_type_patch_writes_nothing(
    api: TestClient,
    db_session: Session,
    owner: User,
    event: Event,
    ticket_type: TicketType,
    body: dict,
) -> None:
    _patch_type(api, owner, ticket_type, body)

    assert _rows(db_session, event.id) == []


def test_refused_type_patch_writes_nothing(
    api: TestClient, db_session: Session, owner: User, event: Event, ticket_type: TicketType
) -> None:
    url = f"/events/{event.id}/ticket-types/{ticket_type.id}"
    response = api.patch(url, json={"price_kzt": 0}, headers=_headers(owner))  # paid needs > 0

    assert response.status_code == 409
    assert _rows(db_session, event.id) == []


# --- check-in ---


def test_check_in_and_undo_are_audited(
    api: TestClient, db_session: Session, owner: User, event: Event
) -> None:
    free = create_free_ticket_type(db_session, event)
    ticket = _register(db_session, create_user(db_session), free)

    scan = api.post(
        "/checkin/scan",
        json={"event_id": str(event.id), "qr_token": ticket.qr_token},
        headers=_headers(owner),
    )
    assert scan.json()["result"] == "checked_in"
    undo = api.post(f"/checkin/tickets/{ticket.id}/undo", headers=_headers(owner))
    assert undo.status_code == 200

    record = db_session.scalars(
        select(CheckInRecord).where(CheckInRecord.ticket_id == ticket.id)
    ).one()
    for action_type in ("ticket.checked_in", "ticket.check_in_reversed"):
        [row] = _rows(db_session, event.id, action_type)
        assert (row.entity_type, row.entity_id, row.actor_user_id) == (
            "ticket",
            ticket.id,
            owner.id,
        )
        assert row.details == {"check_in_record_id": str(record.id)}


def test_refused_scans_write_no_check_in_row(
    api: TestClient, db_session: Session, owner: User, event: Event
) -> None:
    free = create_free_ticket_type(db_session, event)
    ticket = _register(db_session, create_user(db_session), free)
    body = {"event_id": str(event.id), "qr_token": ticket.qr_token}

    first = api.post("/checkin/scan", json=body, headers=_headers(owner)).json()["result"]
    second = api.post("/checkin/scan", json=body, headers=_headers(owner)).json()["result"]
    invalid = api.post(
        "/checkin/scan",
        json={"event_id": str(event.id), "qr_token": "nope"},
        headers=_headers(owner),
    ).json()["result"]

    assert (first, second, invalid) == ("checked_in", "already_used", "invalid")
    assert len(_rows(db_session, event.id, "ticket.checked_in")) == 1


# --- real flow: every action in its own transaction, read back through the timeline ---


def _committed_owner_event() -> dict:
    with SessionLocal() as s:
        organizer = create_organizer(s)
        draft = create_event(s, organizer, status=EventStatus.DRAFT)
        draft.capacity = 100
        paid = create_ticket_type(s, draft, name="VIP", price_kzt=5000, quantity_total=50)
        free = create_free_ticket_type(s, draft, name="Free", quantity_total=10)
        buyer = create_user(s)
        s.commit()
        return {
            "event": draft.id,
            "venue": draft.venue_id,
            "organizer": organizer.id,
            "owner": organizer.user_id,
            "buyer": buyer.id,
            "paid": paid.id,
            "free": free.id,
        }


def _cleanup(ids: dict) -> None:
    with SessionLocal() as s:
        order_ids = select(Order.id).where(Order.event_id == ids["event"])
        item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        ticket_ids = select(Ticket.id).where(Ticket.order_item_id.in_(item_ids))
        s.execute(delete(CheckInRecord).where(CheckInRecord.ticket_id.in_(ticket_ids)))
        s.execute(delete(AuditLog).where(AuditLog.event_id == ids["event"]))
        s.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        s.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
        s.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        s.execute(delete(Order).where(Order.event_id == ids["event"]))
        s.execute(delete(TicketType).where(TicketType.event_id == ids["event"]))
        s.execute(delete(Event).where(Event.id == ids["event"]))
        s.execute(delete(Venue).where(Venue.id == ids["venue"]))
        s.execute(delete(OrganizerProfile).where(OrganizerProfile.id == ids["organizer"]))
        s.execute(delete(User).where(User.id.in_([ids["owner"], ids["buyer"]])))
        s.commit()


def test_wired_actions_appear_in_the_timeline_in_order(client: TestClient) -> None:
    ids = _committed_owner_event()
    try:
        owner = {"Authorization": f"Bearer {create_access_token(ids['owner'])}"}
        buyer = {"Authorization": f"Bearer {create_access_token(ids['buyer'])}"}
        e, paid_url = ids["event"], f"/events/{ids['event']}/ticket-types/{ids['paid']}"

        assert client.post(f"/events/{e}/publish", headers=owner).status_code == 200
        assert client.patch(f"/events/{e}", json={"capacity": 90}, headers=owner).status_code == 200
        assert client.patch(paid_url, json={"price_kzt": 6000}, headers=owner).status_code == 200
        assert client.patch(paid_url, json={"quantity_total": 40}, headers=owner).status_code == 200
        order = client.post(
            f"/events/{e}/registrations",
            json={
                "items": [{"ticket_type_id": str(ids["free"]), "quantity": 1}],
                "attendees": [{"name": "Guest"}],
            },
            headers=buyer,
        ).json()
        ticket = order["tickets"][0]
        scan = client.post(
            "/checkin/scan",
            json={"event_id": str(e), "qr_token": ticket["qr_token"]},
            headers=owner,
        )
        assert scan.json()["result"] == "checked_in"
        undo = client.post(f"/checkin/tickets/{ticket['id']}/undo", headers=owner)
        assert undo.status_code == 200

        timeline = client.get(f"/events/{e}/timeline", headers=owner).json()

        assert [(i["action_type"], i["entity_type"]) for i in timeline["items"]] == [
            ("event.published", "event"),
            ("event.capacity_changed", "event"),
            ("ticket_type.price_changed", "ticket_type"),
            ("ticket_type.quantity_changed", "ticket_type"),
            ("ticket.checked_in", "ticket"),
            ("ticket.check_in_reversed", "ticket"),
        ]
        assert {i["actor_user_id"] for i in timeline["items"]} == {str(ids["owner"])}
        created = [i["created_at"] for i in timeline["items"]]
        assert created == sorted(created) and len(set(created)) == 6
    finally:
        _cleanup(ids)


# --- two gates, one ticket (real connections) ---


def _two_gates(bypass_lock: bool, monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], int]:
    """Two gates scan the same QR at the same moment; returns (results, checked_in rows).

    With bypass_lock, both gates read the ticket as valid without the row lock, so the
    unique index on check_in_records is what refuses the loser, at the flush.
    """
    ids = _committed_owner_event()
    try:
        with SessionLocal() as s:
            event = s.get_one(Event, ids["event"])
            event.status = EventStatus.PUBLISHED
            s.commit()
            buyer, free = s.get_one(User, ids["buyer"]), s.get_one(TicketType, ids["free"])
            token = _register(s, buyer, free).qr_token
        if bypass_lock:
            read_both = threading.Barrier(2)

            def find_without_lock(session: Session, qr_token: str) -> Ticket | None:
                ticket = session.scalar(select(Ticket).where(Ticket.qr_token == qr_token))
                read_both.wait(10)  # both gates have seen `valid` before either writes
                return ticket

            monkeypatch.setattr(checkin_repository, "find_ticket_for_update", find_without_lock)

        start = threading.Barrier(2)
        results: list[object] = []

        def gate() -> None:
            with SessionLocal() as session:
                owner = session.get_one(User, ids["owner"])
                start.wait(10)
                try:
                    results.append(checkin_service.scan(session, owner, ids["event"], token).result)
                except Exception as exc:  # noqa: BLE001 - a 500 in the API
                    results.append(exc)

        gates = [threading.Thread(target=gate) for _ in range(2)]
        for thread in gates:
            thread.start()
        for thread in gates:
            thread.join(timeout=30)
        with SessionLocal() as check:
            rows = len(_rows(check, ids["event"], "ticket.checked_in"))
        return results, rows
    finally:
        _cleanup(ids)


@pytest.mark.slow
@pytest.mark.parametrize("bypass_lock", [False, True], ids=["row lock", "unique index"])
def test_two_gates_one_ticket(bypass_lock: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    results, rows = _two_gates(bypass_lock, monkeypatch)

    assert sorted(map(str, results)) == [ScanResult.ALREADY_USED, ScanResult.CHECKED_IN]
    assert rows == 1
