"""Demo seed (BE-23): idempotent, renames old demo events, finishes interrupted runs, and its
tickets work at the door.

Every test seeds its own demo accounts and promo code (the `fresh` fixture), so a database
that was already seeded by hand does not change what the tests see.
"""

import dataclasses
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, PayoutAccount, StaffAssignment, User
from app.modules.identity.security import create_access_token, verify_password
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Order, Ticket, TicketType
from app.modules.orders.qr import verify_ticket_token
from app.modules.payments.models import Payment, Refund
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
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
    PromotionalCampaign,
    PromoCode,
    PromoRedemption,
)


def _counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in COUNTED
    }


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> str:
    """Unique demo emails and promo code for this test; returns the promo code."""
    tag = uuid.uuid4().hex[:8]
    for name in ("ORGANIZER_EMAIL", "SCANNER_EMAIL", "BUYER_EMAIL"):
        monkeypatch.setattr(seed_demo, name, f"{tag}.{name.lower()}@example.com")
    code = f"T{tag}".upper()
    monkeypatch.setattr(
        seed_demo, "PROMOS", tuple(dataclasses.replace(p, code=code) for p in seed_demo.PROMOS)
    )
    monkeypatch.setattr(
        seed_demo,
        "TICKETS",
        tuple(
            dataclasses.replace(t, promo_code=code) if t.promo_code else t
            for t in seed_demo.TICKETS
        ),
    )
    return code


pytestmark = postgres_only


def test_seed_twice_creates_nothing_new(db_session: Session, fresh: str) -> None:
    first = seed_demo.run(db_session, password=PASSWORD)
    after_first = _counts(db_session)
    second = seed_demo.run(db_session, password=PASSWORD)

    assert _counts(db_session) == after_first
    assert second.created == []
    assert second.warnings == []
    assert [t.ticket_id for t in second.tickets] == [t.ticket_id for t in first.tickets]
    assert {e.id for e in second.events.values()} == {e.id for e in first.events.values()}

    assert len(first.venues) == len(seed_demo.VENUES)
    assert all(v.timezone == "Asia/Almaty" for v in first.venues)
    for event in first.events.values():
        assert event.status == EventStatus.PUBLISHED
        assert event.title.startswith("Demo: ")
    assert first.events["paid"].paid_sales_active
    assert first.events["multi"].paid_sales_active
    assert not first.events["free"].paid_sales_active

    statuses = {t.label: t.status for t in first.tickets}
    assert statuses == {spec.label: spec.status for spec in seed_demo.TICKETS}
    for row in first.tickets:
        assert verify_ticket_token(row.qr_token) == row.ticket_id

    buyer = db_session.scalar(select(User).where(User.email == seed_demo.BUYER_EMAIL))
    assert verify_password(PASSWORD, buyer.password_hash)


def test_cancel_and_refund_go_through_the_services(db_session: Session, fresh: str) -> None:
    result = seed_demo.run(db_session, password=PASSWORD)
    tickets = {t.label: t for t in result.tickets}

    def order_of(label: str) -> Order:
        return db_session.get(Ticket, tickets[label].ticket_id).order_item.order

    def actions(order: Order) -> set[str]:
        return set(
            db_session.scalars(select(AuditLog.action_type).where(AuditLog.entity_id == order.id))
        )

    cancelled = order_of("free-cancelled")
    assert cancelled.status == OrderStatus.CANCELLED
    assert "order.cancelled" in actions(cancelled)
    refunded = order_of("paid-refunded")
    assert refunded.status == OrderStatus.REFUNDED
    assert "order.refunded" in actions(refunded)


def test_promo_code_and_discounted_ticket(db_session: Session, fresh: str) -> None:
    result = seed_demo.run(db_session, password=PASSWORD)
    [promo] = result.promos
    assert promo.code == fresh
    assert promo.qr_token.startswith("pc1.")
    assert promo.qr_link.endswith(f"?promo={promo.qr_token}")

    ticket = next(t for t in result.tickets if t.label == "paid-promo")
    order = db_session.get(Ticket, ticket.ticket_id).order_item.order
    spec = next(p for p in seed_demo.PROMOS if p.code == fresh)
    assert 0 < spec.percent < 100
    assert order.discount_kzt == order.total_kzt * spec.percent // 100


def test_old_titles_are_renamed_not_duplicated(db_session: Session, fresh: str) -> None:
    first = seed_demo.run(db_session, password=PASSWORD)
    # Back to the titles the first seed (#19) used, as on production.
    for spec in seed_demo.EVENTS:
        db_session.execute(
            update(Event)
            .where(Event.id == first.events[spec.key].id)
            .values(title=spec.old_titles[0])
        )
    db_session.flush()
    before = _counts(db_session)

    second = seed_demo.run(db_session, password=PASSWORD)

    after = _counts(db_session)
    assert after["events"] == before["events"]
    assert after["tickets"] == before["tickets"]
    for spec in seed_demo.EVENTS:
        event = db_session.get(Event, first.events[spec.key].id)
        assert event.title == spec.title
        assert second.events[spec.key].id == event.id
    assert sum(line.startswith("renamed ") for line in second.created) == len(seed_demo.EVENTS)


def test_interrupted_run_is_finished_next_time(
    db_session: Session, fresh: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first run stops right after issuing each ticket: everything stays valid.
    with monkeypatch.context() as m:
        m.setattr(seed_demo, "_bring_to_status", lambda *args, **kwargs: None)
        interrupted = seed_demo.run(db_session, password=PASSWORD)
    assert {t.status for t in interrupted.tickets} == {TicketStatus.VALID}

    finished = seed_demo.run(db_session, password=PASSWORD)

    statuses = {t.label: t.status for t in finished.tickets}
    assert statuses == {spec.label: spec.status for spec in seed_demo.TICKETS}
    assert [t.ticket_id for t in finished.tickets] == [t.ticket_id for t in interrupted.tickets]
    assert seed_demo.run(db_session, password=PASSWORD).created == []


def test_seed_generates_one_password_for_new_accounts(db_session: Session, fresh: str) -> None:
    result = seed_demo.run(db_session, password=None)
    assert result.generated_password
    assert all(a.password_note.startswith("generated") for a in result.accounts)
    for account in result.accounts:
        user = db_session.scalar(select(User).where(User.email == account.email))
        assert verify_password(result.generated_password, user.password_hash)

    again = seed_demo.run(db_session, password=None)
    assert again.generated_password is None
    assert all(a.password_note == "unchanged" for a in again.accounts)


def test_demo_password_is_never_printed(
    db_session: Session, fresh: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = seed_demo.run(db_session, password=PASSWORD)
    seed_demo.print_summary(result, print_tokens=True)
    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert "generated password" not in out


def test_seeded_tickets_at_the_door(
    db_session: Session, session_api: TestClient, fresh: str
) -> None:
    result = seed_demo.run(db_session, password=PASSWORD)
    scanner = db_session.scalar(select(User).where(User.email == seed_demo.SCANNER_EMAIL))
    headers = {"Authorization": f"Bearer {create_access_token(scanner.id)}"}
    tickets = {t.label: t for t in result.tickets}
    free_event = result.events["free"].id
    paid_event = result.events["paid"].id

    def scan(qr: str, event_id=free_event) -> str:
        response = session_api.post(
            "/checkin/scan",
            json={"event_id": str(event_id), "qr_token": qr},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]

    assert scan(tickets["free-valid"].qr_token) == "checked_in"
    assert scan(tickets["free-valid"].qr_token) == "already_used"
    assert scan(tickets["free-checked-in"].qr_token) == "already_used"
    assert scan(tickets["free-cancelled"].qr_token) == "cancelled"
    assert scan(tickets["paid-refunded"].qr_token, paid_event) == "refunded"
    assert scan(tickets["multi-vip"].qr_token) == "wrong_event"
    assert scan(tickets["paid-valid"].qr_token, paid_event) == "checked_in"
    assert scan(tickets["paid-promo"].qr_token, paid_event) == "checked_in"
    [promo] = result.promos
    assert scan(promo.qr_link, paid_event) == "campaign_qr_rejected"

    # --reset-scans brings the demo back for the next scanner run.
    again = seed_demo.run(db_session, password=PASSWORD, reset_scans=True)
    statuses = {t.label: t.status for t in again.tickets}
    assert statuses == {spec.label: spec.status for spec in seed_demo.TICKETS}


def test_qr_png_decodes_to_the_token(tmp_path: Path) -> None:
    token = "tk1.00000000-0000-4000-8000-000000000000.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
    link = "https://ticketoff.store/events/00000000-0000-4000-8000-000000000000?promo=pc1.abc"
    row = seed_demo.TicketOutRow(
        label="sample",
        event_title="Event",
        event_id=None,
        ticket_id=None,
        attendee="A",
        status=TicketStatus.VALID,
        qr_token=token,
    )
    promo = seed_demo.PromoOutRow(
        code="DEMO", campaign="C", event_title="Event", qr_token="pc1.abc", qr_link=link
    )
    seed_demo.write_qr_pngs([row], tmp_path, [promo])
    assert decode_qr_png((tmp_path / "sample-valid.png").read_bytes()) == token
    assert decode_qr_png((tmp_path / "promo-DEMO.png").read_bytes()) == link


def test_main_never_emails_the_demo_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    # BE-18: registration and payment notify the buyer; the seed must not mail the demo
    # addresses, even on a server with EMAIL_PROVIDER=smtp.
    from app.modules.notifications import service as notifications

    seen: list[object] = []

    def fake_run(session: Session, **kwargs: object) -> object:
        seen.append(notifications.get_provider())
        return object()

    monkeypatch.setattr(seed_demo, "run", fake_run)
    monkeypatch.setattr(seed_demo, "print_summary", lambda result, **kwargs: None)

    assert seed_demo.main([]) == 0
    assert isinstance(seen[0], seed_demo.NoEmailProvider)
