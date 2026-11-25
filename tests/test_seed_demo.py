"""Demo seed (BE-23): idempotent, renames old demo events, finishes interrupted runs, and its
tickets work at the door.

Every test seeds its own demo accounts and promo code (the `fresh` fixture), so a database
that was already seeded by hand does not change what the tests see.
"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.events import slugs
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
        assert not event.title.startswith("Demo")  # BE-37: no prefix on the showcase
        assert event.slug.startswith(slugs.slugify(event.title))
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
    renamed = [spec for spec in seed_demo.EVENTS if spec.old_titles]
    # Back to the titles the first seed (#19) used, as on production.
    for spec in renamed:
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
    for spec in renamed:
        event = db_session.get(Event, first.events[spec.key].id)
        assert event.title == spec.title
        assert second.events[spec.key].id == event.id
    assert sum(line.startswith("renamed ") for line in second.created) == len(renamed)


def test_demo_prefix_is_dropped_with_its_slug(db_session: Session, fresh: str) -> None:
    """BE-37, as on production: events titled "Demo: ..." (BE-24) with slugs made from that
    title (BE-35 migration) get the clean title and a clean slug, once."""
    first = seed_demo.run(db_session, password=PASSWORD)
    tag = uuid.uuid4().hex[:6]
    for spec in seed_demo.EVENTS:
        db_session.execute(
            update(Event)
            .where(Event.id == first.events[spec.key].id)
            .values(title=f"Demo: {spec.title}", slug=f"demo-{spec.key}-{tag}")
        )
    db_session.flush()
    before = _counts(db_session)

    second = seed_demo.run(db_session, password=PASSWORD)

    assert _counts(db_session) == before
    for spec in seed_demo.EVENTS:
        event = db_session.get(Event, first.events[spec.key].id)
        assert event.title == spec.title
        assert event.slug.startswith(slugs.slugify(spec.title)), event.slug
    assert sum(line.startswith("slug ") for line in second.created) == len(seed_demo.EVENTS)
    paid = first.events["paid"]
    assert db_session.get(Event, paid.id).slug.startswith("kazakh-symphony-night")

    third = seed_demo.run(db_session, password=PASSWORD)

    assert third.created == [] and _counts(db_session) == before


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


def test_showcase_covers_cities_categories_dates_and_prices(
    db_session: Session, session_api: TestClient, fresh: str
) -> None:
    # BE-24: what the storefront needs. Checked through GET /events like the web does.
    result = seed_demo.run(db_session, password=PASSWORD)
    assert 15 <= len(seed_demo.EVENTS) <= 20
    past = {spec.key for spec in seed_demo.EVENTS if spec.past}
    assert len(past) == 2
    assert {spec.category for spec in seed_demo.EVENTS} == set(seed_demo.CATEGORIES)

    ids = {str(e.id) for e in result.events.values()}

    def listed_ours(**params: object) -> list[dict]:
        # The test database may hold other events: walk every page.
        items, offset = [], 0
        while True:
            page = session_api.get("/events", params={"limit": 100, "offset": offset, **params})
            items += page.json()["items"]
            offset += 100
            if offset >= page.json()["total"]:
                return [item for item in items if item["id"] in ids]

    listed = listed_ours()
    assert len(listed) == len(seed_demo.EVENTS) - len(past)
    venues = {str(v.id): v.city for v in result.venues}
    assert {venues[item["venue_id"]] for item in listed} == {"Алматы", "Астана"}
    assert len({item["category"] for item in listed}) >= 5
    prices = [item["price_from"] for item in listed]
    assert 5 <= prices.count(0) <= 6
    assert all(p is not None and (p == 0 or 500 <= p <= 25000) for p in prices)
    assert sum(item["availability"] == "few_left" for item in listed) in (1, 2)

    almaty = listed_ours(city="Алматы")
    assert almaty
    assert {venues[item["venue_id"]] for item in almaty} == {"Алматы"}

    now = datetime.now(seed_demo.TZ)
    today = now.date()
    days = {
        (e.start_at.astimezone(seed_demo.TZ).date() - today).days for e in result.events.values()
    }
    # Late in the evening (Almaty, from 22:00) the earliest start the seed allows for today's
    # event (the next full hour after now + START_LEAD) is already past midnight, so the
    # event moves to tomorrow: then only "tomorrow" is guaranteed.
    earliest = (now + seed_demo.START_LEAD).replace(minute=0, second=0, microsecond=0)
    late_evening = (earliest + timedelta(hours=1)).date() > today
    assert ({1} if late_evening else {0, 1}) <= days
    for key in past:
        event = result.events[key]
        assert event.end_at < datetime.now(UTC)
        assert event.status == EventStatus.PUBLISHED
    statuses = {t.label: t.status for t in result.tickets}
    assert statuses["hackathon-1"] == TicketStatus.CHECKED_IN
    assert statuses["chamber-1"] == TicketStatus.CHECKED_IN


def test_relative_dates_move_events_back(db_session: Session, fresh: str) -> None:
    first = seed_demo.run(db_session, password=PASSWORD)
    # As if seeded ten days ago: everything is ten days earlier now.
    shift = timedelta(days=10)
    for event in first.events.values():
        db_session.execute(
            update(Event)
            .where(Event.id == event.id)
            .values(start_at=Event.start_at - shift, end_at=Event.end_at - shift)
        )
    db_session.flush()
    db_session.expire_all()

    plain = seed_demo.run(db_session, password=PASSWORD)
    assert not any(line.startswith("moved") for line in plain.created)

    moved = seed_demo.run(db_session, password=PASSWORD, relative_dates=True)
    assert moved.warnings == []
    today = datetime.now(seed_demo.TZ).date()
    for spec in seed_demo.EVENTS:
        event = db_session.get(Event, first.events[spec.key].id)
        day = event.start_at.astimezone(seed_demo.TZ).date()
        assert day == seed_demo._start_at(spec).date(), spec.key
        if isinstance(spec.day, int) and spec.day > 0:
            assert (day - today).days == spec.day
    before = _counts(db_session)
    again = seed_demo.run(db_session, password=PASSWORD, relative_dates=True)
    assert again.created == []
    assert _counts(db_session) == before


def test_today_event_seeded_late_still_starts_in_the_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    late = datetime(2026, 11, 26, 22, 30, tzinfo=seed_demo.TZ)
    monkeypatch.setattr(seed_demo, "_now", lambda: late.astimezone(UTC))
    today = next(s for s in seed_demo.EVENTS if s.day == 0)
    assert seed_demo._start_at(today) == datetime(2026, 11, 27, 0, 0, tzinfo=seed_demo.TZ)
    tomorrow = next(s for s in seed_demo.EVENTS if s.day == 1)
    assert seed_demo._start_at(tomorrow).date().isoformat() == "2026-11-27"
    # 26.11.2026 is a Thursday: the weekend is 28-29.11.
    saturday = next(s for s in seed_demo.EVENTS if s.day == seed_demo.SATURDAY)
    assert seed_demo._start_at(saturday).date().isoformat() == "2026-11-28"
    past = next(s for s in seed_demo.EVENTS if s.past)
    assert seed_demo._start_at(past) < late
