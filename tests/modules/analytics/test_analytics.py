"""Organizer analytics (BE-21): the numbers, who sees them, and the endpoints.

Every test reads one organizer's own events, so rows of other tests never leak in.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.analytics import service
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.events.schemas import EventBucket
from app.modules.identity.models import OrganizerProfile
from app.modules.identity.security import create_access_token
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.payments import service as payments_service
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from app.modules.promotions.enums import DiscountType
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
    create_user,
)

pytestmark = pytest.mark.usefixtures("three_percent")


@pytest.fixture
def three_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The math below assumes the default 3%, whatever the local .env says."""
    monkeypatch.setattr(get_settings(), "processing_fee_percent", 3)


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    return create_event(db_session, organizer)


def sale(
    session: Session,
    event: Event,
    ticket_type: TicketType,
    *,
    quantity: int = 1,
    status: OrderStatus = OrderStatus.PAID,
    paid: int | None = None,
    refunded: bool = False,
    checked_in: int = 0,
    created_at: datetime | None = None,
    discount: int = 0,
    also: TicketType | None = None,
) -> Order:
    """An order with tickets, and (when `paid`) a succeeded payment, maybe fully refunded.

    `discount` is the promo discount on the order; `also` adds a second item (one ticket of
    another type), so the order has several items.
    """
    order = Order(
        buyer_user_id=create_user(session).id,
        event_id=event.id,
        status=status,
        total_kzt=ticket_type.price_kzt * quantity + (also.price_kzt if also else 0),
        discount_kzt=discount,
    )
    if created_at is not None:
        order.created_at = created_at
    item = OrderItem(
        ticket_type_id=ticket_type.id, quantity=quantity, unit_price_kzt=ticket_type.price_kzt
    )
    order.items.append(item)
    for n in range(quantity):
        item.tickets.append(
            Ticket(
                attendee=Attendee(name=f"Guest {n}", order=order),
                qr_token=uuid.uuid4().hex,
                status=TicketStatus.CHECKED_IN if n < checked_in else TicketStatus.VALID,
            )
        )
    if also is not None:
        extra = OrderItem(ticket_type_id=also.id, quantity=1, unit_price_kzt=also.price_kzt)
        order.items.append(extra)
        extra.tickets.append(
            Ticket(attendee=Attendee(name="Extra", order=order), qr_token=uuid.uuid4().hex)
        )
    session.add(order)
    session.flush()
    if paid:
        payment = Payment(
            purpose=PaymentPurpose.TICKET_ORDER,
            order_id=order.id,
            amount_kzt=paid,
            status=PaymentStatus.SUCCEEDED,
        )
        session.add(payment)
        session.flush()
        if refunded:
            session.add(
                Refund(payment_id=payment.id, amount_kzt=paid, status=RefundStatus.SUCCEEDED)
            )
            session.flush()
    return order


def stats(session: Session, organizer: OrganizerProfile, event: Event):
    page = service.list_history(session, organizer, bucket=None, limit=100, offset=0)
    return next(row for row in page.items if row.event_id == event.id)


# --- the numbers of one event ---


def test_an_event_without_sales_is_all_zeros(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    row = stats(db_session, organizer, event)

    assert (row.orders, row.tickets_sold, row.checked_in, row.check_in_percent) == (0, 0, 0, 0.0)
    assert (row.gross_kzt, row.fees_kzt, row.refunds_kzt, row.estimated_payout_kzt) == (0, 0, 0, 0)
    assert (row.title, row.status, row.capacity) == (event.title, event.status, event.capacity)


def test_only_confirmed_orders_count_as_sold(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    sale(db_session, event, ticket_type, quantity=2, paid=10_000)
    sale(db_session, event, ticket_type, quantity=1, paid=5_000)
    sale(db_session, event, ticket_type, quantity=4, status=OrderStatus.PENDING)
    sale(db_session, event, ticket_type, quantity=3, status=OrderStatus.CANCELLED)
    sale(db_session, event, ticket_type, quantity=2, status=OrderStatus.REFUNDED)

    row = stats(db_session, organizer, event)

    assert (row.orders, row.tickets_sold) == (2, 3)
    assert (row.gross_kzt, row.fees_kzt, row.refunds_kzt) == (15_000, 450, 0)
    assert row.estimated_payout_kzt == 14_550


def test_a_refunded_order_costs_its_fee_and_is_not_a_sold_ticket(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    sale(db_session, event, ticket_type, quantity=2, paid=10_000)
    sale(
        db_session,
        event,
        ticket_type,
        status=OrderStatus.REFUNDED,
        paid=5_000,
        refunded=True,
    )

    row = stats(db_session, organizer, event)

    assert (row.tickets_sold, row.gross_kzt, row.refunds_kzt) == (2, 15_000, 5_000)
    assert row.estimated_payout_kzt == 15_000 - 450 - 5_000


def test_free_orders_count_as_tickets_but_bring_no_money(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    free = create_free_ticket_type(db_session, event)
    sale(db_session, event, free, quantity=3, status=OrderStatus.FREE_CONFIRMED)

    row = stats(db_session, organizer, event)

    assert (row.orders, row.tickets_sold, row.gross_kzt) == (1, 3, 0)


def test_check_ins_and_percent(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=1000)
    sale(db_session, event, ticket_type, quantity=3, paid=3_000, checked_in=1)
    sale(db_session, event, ticket_type, quantity=1, paid=1_000)

    row = stats(db_session, organizer, event)

    assert (row.tickets_sold, row.checked_in, row.check_in_percent) == (4, 1, 25.0)


def test_the_row_matches_the_payment_summary(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    sale(db_session, event, ticket_type, quantity=2, paid=10_050)
    sale(db_session, event, ticket_type, quantity=1, paid=5_000, refunded=True)

    row = stats(db_session, organizer, event)
    summary = payments_service.get_summary(db_session, event)

    assert (row.gross_kzt, row.fees_kzt, row.refunds_kzt, row.estimated_payout_kzt) == (
        summary.gross_kzt,
        summary.fees_kzt,
        summary.refunds_kzt,
        summary.estimated_payout_kzt,
    )


# --- several events, several organizers ---


def test_overview_adds_up_my_events_and_ignores_other_organizers(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    second = create_event(db_session, organizer)
    stranger_event = create_event(db_session, create_organizer(db_session))
    paid_type = create_ticket_type(db_session, event, price_kzt=50)
    sale(db_session, event, paid_type, paid=50)
    sale(db_session, second, create_ticket_type(db_session, second, price_kzt=50), paid=50)
    sale(
        db_session,
        stranger_event,
        create_ticket_type(db_session, stranger_event, price_kzt=9_999),
        paid=9_999,
    )

    overview = service.get_overview(db_session, organizer)

    assert overview.events.total == 2
    assert (overview.totals.orders, overview.totals.tickets_sold) == (2, 2)
    # 3% of 50 is 1.5 -> 2 for each event, so 4 in total (not 3% of 100 = 3).
    assert (overview.totals.gross_kzt, overview.totals.fees_kzt) == (100, 4)
    assert overview.totals.estimated_payout_kzt == 96
    assert overview.fee_percent == 3


def test_overview_counts_events_per_bucket(
    db_session: Session, organizer: OrganizerProfile
) -> None:
    now = datetime.now(UTC)
    create_event(db_session, organizer)  # upcoming
    past = create_event(db_session, organizer)
    past.start_at = now - timedelta(days=10)
    past.end_at = now - timedelta(days=9)
    cancelled = create_event(db_session, organizer, status=EventStatus.CANCELLED)
    cancelled.start_at = now + timedelta(days=3)
    db_session.flush()

    counts = service.get_overview(db_session, organizer).events

    assert counts.model_dump() == {
        "total": 3,
        "upcoming": 1,
        "active": 0,
        "completed": 1,
        "cancelled": 1,
        "draft": 0,
    }


def test_history_is_newest_first_filterable_and_paged(
    db_session: Session, organizer: OrganizerProfile
) -> None:
    now = datetime.now(UTC)
    events = []
    for days in (-30, -20, 10):
        item = create_event(db_session, organizer)
        item.start_at = now + timedelta(days=days)
        item.end_at = item.start_at + timedelta(hours=2)
        events.append(item)
    db_session.flush()
    old, middle, future = events

    everything = service.list_history(db_session, organizer, bucket=None, limit=10, offset=0)
    completed = service.list_history(
        db_session, organizer, bucket=EventBucket.COMPLETED, limit=10, offset=0
    )
    second_page = service.list_history(db_session, organizer, bucket=None, limit=1, offset=1)

    assert [r.event_id for r in everything.items] == [future.id, middle.id, old.id]
    assert [r.event_id for r in completed.items] == [middle.id, old.id]
    assert completed.total == 2
    assert [r.event_id for r in second_page.items] == [middle.id]
    assert (second_page.total, second_page.limit, second_page.offset) == (3, 1, 1)


# --- one event in detail ---


def test_ticket_types_show_sold_and_sales_before_discounts(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    cheap = create_ticket_type(db_session, event, name="Cheap", price_kzt=1000)
    vip = create_ticket_type(db_session, event, name="VIP", price_kzt=9000)
    hidden = create_ticket_type(db_session, event, name="Hidden", price_kzt=500, is_hidden=True)
    sale(db_session, event, cheap, quantity=3, paid=3_000)
    sale(db_session, event, vip, quantity=1, paid=9_000)
    sale(db_session, event, vip, quantity=5, status=OrderStatus.PENDING)  # not sold

    detail = service.get_event_analytics(db_session, event)

    rows = {r.name: r for r in detail.ticket_types}
    assert (rows["Cheap"].sold, rows["Cheap"].sales_kzt) == (3, 3_000)
    assert (rows["VIP"].sold, rows["VIP"].sales_kzt) == (1, 9_000)
    assert (rows["Hidden"].sold, rows["Hidden"].is_hidden) == (0, True)
    assert [r.ticket_type_id for r in detail.ticket_types][0] == hidden.id  # cheapest first
    assert detail.event.tickets_sold == 4


def test_daily_sales_use_the_venue_time_zone(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=1000)
    # The test venue is in Asia/Almaty (UTC+5): 20:00 UTC on the 10th is 01:00 on the 11th.
    sale(
        db_session,
        event,
        ticket_type,
        quantity=2,
        paid=2_000,
        created_at=datetime(2030, 3, 10, 20, 0, tzinfo=UTC),
    )
    sale(
        db_session,
        event,
        ticket_type,
        quantity=1,
        paid=1_000,
        created_at=datetime(2030, 3, 10, 10, 0, tzinfo=UTC),
    )
    sale(
        db_session,
        event,
        ticket_type,
        quantity=1,
        paid=1_000,
        created_at=datetime(2030, 3, 11, 4, 0, tzinfo=UTC),
    )
    sale(db_session, event, ticket_type, quantity=9, status=OrderStatus.PENDING)

    detail = service.get_event_analytics(db_session, event)

    assert detail.timezone == "Asia/Almaty"
    assert [
        (d.day.isoformat(), d.orders, d.tickets_sold, d.sales_kzt) for d in detail.daily_sales
    ] == [
        ("2030-03-10", 1, 1, 1_000),
        ("2030-03-11", 2, 3, 3_000),
    ]


def test_daily_sales_after_discounts_count_a_multi_item_order_once(
    db_session: Session, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    other = create_ticket_type(db_session, event, name="Other", price_kzt=1000)
    day = datetime(2030, 3, 10, 10, 0, tzinfo=UTC)
    # One order, two items: 5000 + 1000 - 1000 discount = 5000, two tickets.
    sale(db_session, event, ticket_type, also=other, paid=5_000, discount=1_000, created_at=day)

    (only,) = service.get_event_analytics(db_session, event).daily_sales

    assert (only.orders, only.tickets_sold, only.sales_kzt) == (1, 2, 5_000)


# --- discounts, inventory, drafts ---


def test_discounts_are_shown_and_gross_stays_what_was_paid(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    # Two tickets of 5000 with a 2000 promo discount: the buyer paid 8000.
    sale(db_session, event, ticket_type, quantity=2, paid=8_000, discount=2_000)
    sale(db_session, event, ticket_type, quantity=1, paid=5_000)
    sale(db_session, event, ticket_type, status=OrderStatus.PENDING, discount=999)  # not counted

    row = stats(db_session, organizer, event)
    summary = payments_service.get_summary(db_session, event)

    assert (row.gross_kzt, row.discounts_kzt) == (13_000, 2_000)
    assert row.gross_kzt == summary.gross_kzt


def test_remaining_and_percent_sold_come_from_the_ticket_types(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    assert event.capacity is None  # capacity may be empty, the numbers must not depend on it
    vip = create_ticket_type(db_session, event, name="VIP", price_kzt=9000, quantity_total=10)
    hidden = create_ticket_type(
        db_session, event, name="Hidden", price_kzt=500, quantity_total=30, is_hidden=True
    )
    sale(db_session, event, vip, quantity=4, paid=36_000)
    sale(db_session, event, hidden, quantity=6, paid=3_000)
    sale(db_session, event, vip, quantity=9, status=OrderStatus.PENDING)  # not sold

    row = stats(db_session, organizer, event)

    assert (row.tickets_total, row.tickets_sold, row.remaining) == (40, 10, 30)
    assert row.percent_sold == 25.0


def test_an_event_without_ticket_types_has_no_inventory(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    row = stats(db_session, organizer, event)

    assert (row.tickets_total, row.remaining, row.percent_sold) == (0, 0, 0.0)


def test_event_buckets_add_up_and_drafts_are_counted(
    db_session: Session, organizer: OrganizerProfile
) -> None:
    now = datetime.now(UTC)
    create_event(db_session, organizer)  # published, upcoming
    create_event(db_session, organizer, status=EventStatus.DRAFT)  # draft, upcoming
    old_draft = create_event(db_session, organizer, status=EventStatus.DRAFT)
    old_draft.start_at = now - timedelta(days=5)
    old_draft.end_at = now - timedelta(days=4)
    db_session.flush()

    counts = service.get_overview(db_session, organizer).events

    assert counts.total == 3
    assert counts.draft == 2
    assert counts.total == counts.upcoming + counts.active + counts.completed + counts.cancelled


# --- campaigns ---


def promo(
    session: Session,
    event: Event,
    name: str,
    orders: list[tuple[Order, int]],
    *,
    value: int = 20,
) -> PromotionalCampaign:
    """A campaign with one code, redeemed on the given (order, discount) pairs."""
    campaign = PromotionalCampaign(
        event_id=event.id, name=name, discount_type=DiscountType.PERCENT, discount_value=value
    )
    session.add(campaign)
    session.flush()
    code = PromoCode(
        campaign_id=campaign.id, code=uuid.uuid4().hex[:10], qr_token=f"pc1.{uuid.uuid4().hex}"
    )
    session.add(code)
    session.flush()
    for order, discount in orders:
        session.add(
            PromoRedemption(promo_code_id=code.id, order_id=order.id, discount_kzt=discount)
        )
    session.flush()
    return campaign


def test_campaign_report_counts_only_confirmed_orders(db_session: Session, event: Event) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    other_type = create_ticket_type(db_session, event, name="Other", price_kzt=1000)
    # 10 000 - 2 000 = 8 000 paid, 2 tickets, then a second order with two items:
    # (5000 + 1000) - 1000 = 5 000 paid, 2 tickets in 2 items (must be counted once).
    first = sale(db_session, event, ticket_type, quantity=2, paid=8_000, discount=2_000)
    second = sale(
        db_session, event, ticket_type, quantity=1, also=other_type, paid=5_000, discount=1_000
    )
    pending = sale(db_session, event, ticket_type, status=OrderStatus.PENDING, discount=500)
    cancelled = sale(db_session, event, ticket_type, status=OrderStatus.CANCELLED, discount=500)
    refunded = sale(
        db_session,
        event,
        ticket_type,
        status=OrderStatus.REFUNDED,
        paid=4_000,
        discount=1_000,
        refunded=True,
    )
    promo(
        db_session,
        event,
        "Spring",
        [(first, 2_000), (second, 1_000), (pending, 500), (cancelled, 500), (refunded, 1_000)],
    )
    unused = promo(db_session, event, "Unused", [])

    detail = service.get_event_analytics(db_session, event)

    rows = {c.name: c for c in detail.campaigns}
    spring = rows["Spring"]
    assert (spring.redemptions, spring.orders, spring.tickets_sold) == (2, 2, 4)
    assert (spring.gross_kzt, spring.discount_total_kzt, spring.net_kzt) == (13_000, 3_000, 13_000)
    assert spring.discount_type == DiscountType.PERCENT
    # A campaign nobody used is still listed, with zeros.
    assert rows["Unused"].campaign_id == unused.id
    assert (rows["Unused"].redemptions, rows["Unused"].gross_kzt, rows["Unused"].net_kzt) == (
        0,
        0,
        0,
    )


def test_campaign_net_subtracts_refunds_of_its_orders(db_session: Session, event: Event) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    # Still counted as sold (paid) but with a succeeded refund on its payment.
    order = sale(db_session, event, ticket_type, quantity=2, paid=8_000, discount=2_000)
    payment = db_session.scalars(select(Payment).where(Payment.order_id == order.id)).one()
    db_session.add(Refund(payment_id=payment.id, amount_kzt=3_000, status=RefundStatus.SUCCEEDED))
    db_session.add(Refund(payment_id=payment.id, amount_kzt=700, status=RefundStatus.FAILED))
    db_session.flush()
    promo(db_session, event, "Spring", [(order, 2_000)])

    (spring,) = service.get_event_analytics(db_session, event).campaigns

    assert (spring.gross_kzt, spring.net_kzt) == (8_000, 5_000)


def test_other_events_campaigns_are_not_listed(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    elsewhere = create_event(db_session, organizer)
    promo(db_session, elsewhere, "Elsewhere", [])

    assert service.get_event_analytics(db_session, event).campaigns == []


# --- endpoints ---


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def headers(organizer: OrganizerProfile) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(organizer.user_id)}"}


def test_the_three_endpoints_for_the_owner(
    api: TestClient, db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, price_kzt=5000)
    sale(db_session, event, ticket_type, quantity=2, paid=10_000, checked_in=1)

    overview = api.get("/organizer/analytics/overview", headers=headers(organizer))
    history = api.get("/organizer/analytics/events", headers=headers(organizer))
    detail = api.get(f"/organizer/analytics/events/{event.id}", headers=headers(organizer))

    assert (overview.status_code, history.status_code, detail.status_code) == (200, 200, 200)
    assert overview.json()["totals"]["tickets_sold"] == 2
    assert overview.json()["totals"]["gross_kzt"] == 10_000
    assert overview.json()["is_simulated"] is True
    assert [i["event_id"] for i in history.json()["items"]] == [str(event.id)]
    assert history.json()["items"][0]["check_in_percent"] == 50.0
    assert detail.json()["event"]["estimated_payout_kzt"] == 9_700
    assert [t["sold"] for t in detail.json()["ticket_types"]] == [2]


def test_a_bad_bucket_or_page_is_422(api: TestClient, organizer: OrganizerProfile) -> None:
    for query in ("bucket=nope", "limit=0", "limit=101", "offset=-1"):
        response = api.get(f"/organizer/analytics/events?{query}", headers=headers(organizer))
        assert response.status_code == 422


def test_other_organizers_event_is_404(api: TestClient, db_session: Session, event: Event) -> None:
    stranger = create_organizer(db_session)

    response = api.get(f"/organizer/analytics/events/{event.id}", headers=headers(stranger))

    assert response.status_code == 404


def test_a_plain_user_is_403_and_no_login_is_401(
    api: TestClient, db_session: Session, event: Event
) -> None:
    plain = {"Authorization": f"Bearer {create_access_token(create_user(db_session).id)}"}

    for path in (
        "/organizer/analytics/overview",
        "/organizer/analytics/events",
        f"/organizer/analytics/events/{event.id}",
    ):
        assert api.get(path, headers=plain).status_code == 403
        assert api.get(path).status_code == 401
