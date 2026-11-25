"""Read-only aggregates for the organizer analytics (BE-21).

"Sold" means what the rest of the app means: orders that are paid or free-confirmed
(confirmed_orders_clause). Money comes from succeeded ticket payments and succeeded refunds.
Each figure is its own grouped query instead of one big join: a payment with several refunds,
or an order with several items, would otherwise be counted more than once.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Order, OrderItem, Ticket, TicketType
from app.modules.orders.repository import confirmed_orders_clause
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign


@dataclass(frozen=True)
class Filters:
    """What the analytics are narrowed to (R-118). The default narrows nothing.

    start / end: half-open range [start, end). Sales and money look at the order's date
    (orders.created_at: a paid order is paid within its checkout hold, a free one is confirmed
    at once), refunds included, so net = gross - refunds of the same orders. Check-ins look at
    the scan time (check_in_records.checked_in_at, reversed scans left out).
    ticket_type_id: only that type's tickets. Money of an order with several types is shared
    out by line value (quantity x unit price), rounded down per order.
    """

    start: datetime | None = None
    end: datetime | None = None
    ticket_type_id: uuid.UUID | None = None

    @property
    def has_period(self) -> bool:
        return self.start is not None or self.end is not None


NO_FILTERS = Filters()


def _in_period(column: ColumnElement, filters: Filters) -> list[ColumnElement[bool]]:
    clauses = []
    if filters.start is not None:
        clauses.append(column >= filters.start)
    if filters.end is not None:
        clauses.append(column < filters.end)
    return clauses


def _lines(filters: Filters) -> Subquery:
    """Per order: tickets and line value of the chosen type (of all lines without a type
    filter), and the value of all its lines. Orders with no ticket of the type are absent."""
    value = OrderItem.quantity * OrderItem.unit_price_kzt
    if filters.ticket_type_id is None:
        tickets = func.sum(OrderItem.quantity)
        part = func.sum(value)
    else:
        match = OrderItem.ticket_type_id == filters.ticket_type_id
        tickets = func.sum(case((match, OrderItem.quantity), else_=0))
        part = func.sum(case((match, value), else_=0))
    return (
        select(
            OrderItem.order_id.label("order_id"),
            tickets.label("tickets"),
            part.label("value"),
            func.sum(value).label("all_value"),
        )
        .group_by(OrderItem.order_id)
        .having(tickets > 0)
        .subquery()
    )


def _share(amount: ColumnElement, lines: Subquery, filters: Filters) -> ColumnElement:
    """The chosen ticket type's part of an order-level amount; the amount itself without a
    type filter. Integer division, so an order of one type keeps its exact amount."""
    if filters.ticket_type_id is None:
        return amount
    return case(
        (lines.c.all_value > 0, amount * lines.c.value // lines.c.all_value),
        else_=0,
    )


def orders_and_tickets(
    session: Session, event_ids: Iterable[uuid.UUID], filters: Filters = NO_FILTERS
) -> dict[uuid.UUID, tuple[int, int]]:
    """event id -> (confirmed orders, tickets in them). Events with no sales are absent."""
    ids = list(event_ids)
    if not ids:
        return {}
    lines = _lines(filters)
    stmt = (
        select(Order.event_id, func.count(Order.id), func.sum(lines.c.tickets))
        .select_from(Order)
        .join(lines, lines.c.order_id == Order.id)
        .where(
            Order.event_id.in_(ids),
            confirmed_orders_clause(),
            *_in_period(Order.created_at, filters),
        )
        .group_by(Order.event_id)
    )
    return {
        event_id: (int(orders), int(tickets)) for event_id, orders, tickets in session.execute(stmt)
    }


def discounts_kzt(
    session: Session, event_ids: Iterable[uuid.UUID], filters: Filters = NO_FILTERS
) -> dict[uuid.UUID, int]:
    """event id -> promo discounts given on confirmed orders (what the promos cost)."""
    ids = list(event_ids)
    if not ids:
        return {}
    lines = _lines(filters)
    stmt = (
        select(Order.event_id, func.sum(_share(Order.discount_kzt, lines, filters)))
        .select_from(Order)
        .join(lines, lines.c.order_id == Order.id)
        .where(
            Order.event_id.in_(ids),
            confirmed_orders_clause(),
            *_in_period(Order.created_at, filters),
        )
        .group_by(Order.event_id)
    )
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def tickets_available_in_total(
    session: Session, event_ids: Iterable[uuid.UUID], filters: Filters = NO_FILTERS
) -> dict[uuid.UUID, int]:
    """event id -> sum of quantity_total over its ticket types (hidden ones too), or of the
    chosen type only. Inventory has no date, so the period does not apply."""
    ids = list(event_ids)
    if not ids:
        return {}
    stmt = select(TicketType.event_id, func.sum(TicketType.quantity_total)).where(
        TicketType.event_id.in_(ids)
    )
    if filters.ticket_type_id is not None:
        stmt = stmt.where(TicketType.id == filters.ticket_type_id)
    stmt = stmt.group_by(TicketType.event_id)
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def draft_count(session: Session, organizer_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.organizer_id == organizer_id, Event.status == EventStatus.DRAFT)
    )


def gross_kzt(
    session: Session, event_ids: Iterable[uuid.UUID], filters: Filters = NO_FILTERS
) -> dict[uuid.UUID, int]:
    """event id -> succeeded ticket payments. The activation fee is not ticket revenue."""
    ids = list(event_ids)
    if not ids:
        return {}
    lines = _lines(filters)
    stmt = (
        select(Order.event_id, func.sum(_share(Payment.amount_kzt, lines, filters)))
        .select_from(Payment)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Payment.purpose == PaymentPurpose.TICKET_ORDER,
            Payment.status == PaymentStatus.SUCCEEDED,
            Order.event_id.in_(ids),
            *_in_period(Order.created_at, filters),
        )
        .group_by(Order.event_id)
    )
    if filters.ticket_type_id is not None:
        stmt = stmt.join(lines, lines.c.order_id == Order.id)
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def refunds_kzt(
    session: Session, event_ids: Iterable[uuid.UUID], filters: Filters = NO_FILTERS
) -> dict[uuid.UUID, int]:
    """event id -> succeeded refunds of those ticket payments (of orders in the period)."""
    ids = list(event_ids)
    if not ids:
        return {}
    lines = _lines(filters)
    stmt = (
        select(Order.event_id, func.sum(_share(Refund.amount_kzt, lines, filters)))
        .select_from(Refund)
        .join(Payment, Payment.id == Refund.payment_id)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Payment.purpose == PaymentPurpose.TICKET_ORDER,
            Payment.status == PaymentStatus.SUCCEEDED,
            Refund.status == RefundStatus.SUCCEEDED,
            Order.event_id.in_(ids),
            *_in_period(Order.created_at, filters),
        )
        .group_by(Order.event_id)
    )
    if filters.ticket_type_id is not None:
        stmt = stmt.join(lines, lines.c.order_id == Order.id)
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def checked_in(
    session: Session, event_ids: Iterable[uuid.UUID], filters: Filters = NO_FILTERS
) -> dict[uuid.UUID, int]:
    """event id -> tickets already scanned at the door (with a period: scanned in it)."""
    ids = list(event_ids)
    if not ids:
        return {}
    stmt = (
        select(Order.event_id, func.count())
        .select_from(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id.in_(ids), Ticket.status == TicketStatus.CHECKED_IN)
        .group_by(Order.event_id)
    )
    if filters.ticket_type_id is not None:
        stmt = stmt.where(OrderItem.ticket_type_id == filters.ticket_type_id)
    if filters.has_period:
        # At most one active record per ticket (uq_check_in_records_active_ticket).
        stmt = stmt.join(CheckInRecord, CheckInRecord.ticket_id == Ticket.id).where(
            CheckInRecord.is_reversed.is_(False),
            *_in_period(CheckInRecord.checked_in_at, filters),
        )
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def ticket_type_rows(
    session: Session, event_id: uuid.UUID, filters: Filters = NO_FILTERS
) -> list[tuple[TicketType, int, int]]:
    """Every ticket type of the event (hidden ones too), or only the chosen one, with
    (sold, sales before discounts) in the period."""
    sold = (
        select(
            OrderItem.ticket_type_id.label("ticket_type_id"),
            func.sum(OrderItem.quantity).label("sold"),
            func.sum(OrderItem.quantity * OrderItem.unit_price_kzt).label("sales"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.event_id == event_id,
            confirmed_orders_clause(),
            *_in_period(Order.created_at, filters),
        )
        .group_by(OrderItem.ticket_type_id)
        .subquery()
    )
    stmt = (
        select(TicketType, func.coalesce(sold.c.sold, 0), func.coalesce(sold.c.sales, 0))
        .outerjoin(sold, sold.c.ticket_type_id == TicketType.id)
        .where(TicketType.event_id == event_id)
        .order_by(TicketType.price_kzt, TicketType.name, TicketType.id)
    )
    if filters.ticket_type_id is not None:
        stmt = stmt.where(TicketType.id == filters.ticket_type_id)
    return [(tt, int(sold_), int(sales)) for tt, sold_, sales in session.execute(stmt)]


def daily_sales(
    session: Session, event_id: uuid.UUID, filters: Filters = NO_FILTERS
) -> list[tuple[date, int, int, int]]:
    """(day, orders, tickets, sales after discounts) per day, the day in the venue's time zone.

    The zone comes from the venues column, not from a bound value: the same expression has to
    appear in SELECT and GROUP BY, and two bound copies of it are different to Postgres.
    Tickets are summed per order first, so an order with several items is counted once.
    """
    lines = _lines(filters)
    day = cast(func.timezone(Venue.timezone, Order.created_at), Date)
    stmt = (
        select(
            day,
            func.count(Order.id),
            func.sum(lines.c.tickets),
            func.sum(_share(Order.total_kzt - Order.discount_kzt, lines, filters)),
        )
        .select_from(Order)
        .join(lines, lines.c.order_id == Order.id)
        .join(Event, Event.id == Order.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .where(
            Order.event_id == event_id,
            confirmed_orders_clause(),
            *_in_period(Order.created_at, filters),
        )
        .group_by(day)
        .order_by(day)
    )
    return [
        (d, int(orders), int(tickets), int(sales))
        for d, orders, tickets, sales in session.execute(stmt)
    ]


@dataclass(frozen=True)
class CampaignRow:
    campaign: PromotionalCampaign
    redemptions: int
    orders: int
    tickets: int
    gross_kzt: int  # sum of total - discount of the confirmed orders that used the campaign
    discount_kzt: int
    refunds_kzt: int


def campaign_rows(
    session: Session, event_id: uuid.UUID, filters: Filters = NO_FILTERS
) -> list[CampaignRow]:
    """Every campaign of the event (unused ones too) with what its codes did.

    Only confirmed orders in the period count, and with a ticket type only orders with that
    type (money shared out as in Filters). Two grouped queries (orders, refunds) over a
    one-row-per-order subquery, so an order with several items or refunds is never counted
    twice.
    """
    campaigns = list(
        session.scalars(
            select(PromotionalCampaign)
            .where(PromotionalCampaign.event_id == event_id)
            .order_by(PromotionalCampaign.name, PromotionalCampaign.id)
        )
    )
    if not campaigns:
        return []

    lines = _lines(filters)
    used = (
        select(
            PromoCode.campaign_id,
            Order.id.label("order_id"),
            lines.c.tickets,
            lines.c.value,
            lines.c.all_value,
            _share(Order.total_kzt - Order.discount_kzt, lines, filters).label("gross"),
            _share(Order.discount_kzt, lines, filters).label("discount"),
        )
        .select_from(PromoRedemption)
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .join(Order, Order.id == PromoRedemption.order_id)
        .join(lines, lines.c.order_id == Order.id)
        .where(
            Order.event_id == event_id,
            confirmed_orders_clause(),
            *_in_period(Order.created_at, filters),
        )
        .subquery()
    )
    orders = {
        campaign_id: (int(count), int(tickets), int(gross), int(discount))
        for campaign_id, count, tickets, gross, discount in session.execute(
            select(
                used.c.campaign_id,
                func.count(),
                func.sum(used.c.tickets),
                func.sum(used.c.gross),
                func.sum(used.c.discount),
            ).group_by(used.c.campaign_id)
        )
    }
    refunds = {
        campaign_id: int(total)
        for campaign_id, total in session.execute(
            select(used.c.campaign_id, func.sum(_share(Refund.amount_kzt, used, filters)))
            .select_from(used)
            .join(Payment, Payment.order_id == used.c.order_id)
            .join(Refund, Refund.payment_id == Payment.id)
            .where(
                Payment.purpose == PaymentPurpose.TICKET_ORDER,
                Payment.status == PaymentStatus.SUCCEEDED,
                Refund.status == RefundStatus.SUCCEEDED,
            )
            .group_by(used.c.campaign_id)
        )
    }
    rows = []
    for campaign in campaigns:
        count, tickets, gross, discount = orders.get(campaign.id, (0, 0, 0, 0))
        rows.append(
            CampaignRow(
                campaign=campaign,
                redemptions=count,
                orders=count,
                tickets=tickets,
                gross_kzt=gross,
                discount_kzt=discount,
                refunds_kzt=refunds.get(campaign.id, 0),
            )
        )
    return rows
