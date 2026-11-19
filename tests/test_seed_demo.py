"""Demo seed (BE-23): idempotent, and its tickets work at the door."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.audit.models import AuditLog
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, PayoutAccount, StaffAssignment, User
from app.modules.identity.security import create_access_token, verify_password
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Order, Ticket, TicketType
from app.modules.orders.qr import verify_ticket_token
from app.modules.payments.models import Payment, Refund
from app.scripts import seed_demo
from tests.markers import postgres_only
from tests.qr import decode_qr_png

PASSWORD = "demo-password-for-tests"
COUNTED = (
    User,
    OrganizerProfile,
    PayoutAccount,
    Venue,
    Event,
    TicketType,
    StaffAssignment,
    Order,
    Ticket,
    Payment,
    Refund,
    CheckInRecord,
    AuditLog,
)


def _counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in COUNTED
    }


@pytest.fixture
def api(db_session: Session) -> Iterator[TestClient]:
    """The API on the test's own session, so it sees the seed and everything is rolled back."""

    def _session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@postgres_only
def test_seed_twice_creates_nothing_new(db_session: Session) -> None:
    first = seed_demo.run(db_session, password=PASSWORD)
    after_first = _counts(db_session)
    second = seed_demo.run(db_session, password=PASSWORD)

    assert _counts(db_session) == after_first
    assert second.created == []
    assert [t.ticket_id for t in second.tickets] == [t.ticket_id for t in first.tickets]
    assert {e.id for e in second.events.values()} == {e.id for e in first.events.values()}

    assert len(first.venues) == len(seed_demo.VENUES)
    assert all(v.timezone == "Asia/Almaty" for v in first.venues)
    for event in first.events.values():
        assert event.status == EventStatus.PUBLISHED
    assert first.events["paid"].paid_sales_active
    assert first.events["multi"].paid_sales_active
    assert not first.events["free"].paid_sales_active

    statuses = {t.label: t.status for t in first.tickets}
    assert statuses == {spec.label: spec.status for spec in seed_demo.TICKETS}
    for row in first.tickets:
        assert verify_ticket_token(row.qr_token) == row.ticket_id

    buyer = db_session.scalar(select(User).where(User.email == seed_demo.BUYER_EMAIL))
    assert verify_password(PASSWORD, buyer.password_hash)


@postgres_only
def test_seed_generates_one_password_for_new_accounts(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fresh emails, so the accounts are new even if this database was seeded before.
    tag = uuid.uuid4().hex[:8]
    for name in ("ORGANIZER_EMAIL", "SCANNER_EMAIL", "BUYER_EMAIL"):
        monkeypatch.setattr(seed_demo, name, f"{tag}.{name.lower()}@example.com")

    result = seed_demo.run(db_session, password=None)
    assert result.generated_password
    assert all(a.password_note.startswith("generated") for a in result.accounts)
    for account in result.accounts:
        user = db_session.scalar(select(User).where(User.email == account.email))
        assert verify_password(result.generated_password, user.password_hash)

    again = seed_demo.run(db_session, password=None)
    assert again.generated_password is None
    assert all(a.password_note == "unchanged" for a in again.accounts)


@postgres_only
def test_seeded_tickets_at_the_door(db_session: Session, api: TestClient) -> None:
    result = seed_demo.run(db_session, password=PASSWORD)
    scanner = db_session.scalar(select(User).where(User.email == seed_demo.SCANNER_EMAIL))
    headers = {"Authorization": f"Bearer {create_access_token(scanner.id)}"}
    tickets = {t.label: t for t in result.tickets}
    free_event = result.events["free"].id

    def scan(label: str, event_id=free_event) -> str:
        response = api.post(
            "/checkin/scan",
            json={"event_id": str(event_id), "qr_token": tickets[label].qr_token},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]

    if tickets["free-valid"].status == TicketStatus.VALID:
        assert scan("free-valid") == "checked_in"
    assert scan("free-valid") == "already_used"
    assert scan("free-checked-in") == "already_used"
    assert scan("free-cancelled") == "cancelled"
    assert scan("paid-refunded", result.events["paid"].id) == "refunded"
    assert scan("multi-vip") == "wrong_event"
    assert scan("paid-valid", result.events["paid"].id) == "checked_in"

    # --reset-scans brings the demo back for the next scanner run.
    again = seed_demo.run(db_session, password=PASSWORD, reset_scans=True)
    statuses = {t.label: t.status for t in again.tickets}
    assert statuses["free-valid"] == TicketStatus.VALID
    assert statuses["paid-valid"] == TicketStatus.VALID
    assert statuses["free-checked-in"] == TicketStatus.CHECKED_IN


def test_qr_png_decodes_to_the_token(tmp_path: Path) -> None:
    token = "tk1.00000000-0000-4000-8000-000000000000.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
    row = seed_demo.TicketOutRow(
        label="sample",
        event_title="Event",
        event_id=None,
        ticket_id=None,
        attendee="A",
        status=TicketStatus.VALID,
        qr_token=token,
    )
    seed_demo.write_qr_pngs([row], tmp_path)
    assert decode_qr_png((tmp_path / "sample-valid.png").read_bytes()) == token
