"""Read-only aggregates for the organizer analytics (BE-21).

"Sold" means what the rest of the app means: orders that are paid or free-confirmed
(confirmed_orders_clause). Money comes from succeeded ticket payments and succeeded refunds.
Each figure is its own grouped query instead of one big join: a payment with several refunds,
or an order with several items, would otherwise be counted more than once.
"""

import uuid
from collections.abc import Iterable
from datetime import date

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.modules.events.models import Event, Venue
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Order, OrderItem, Ticket, TicketType
from app.modules.orders.repository import confirmed_orders_clause
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund


def orders_and_tickets(
    session: Session, event_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """event id -> (confirmed orders, tickets in them). Events with no sales are absent."""
    ids = list(event_ids)
    if not ids:
        return {}
    stmt = (
        select(Order.event_id, func.count(func.distinct(Order.id)), func.sum(OrderItem.quantity))
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(Order.event_id.in_(ids), confirmed_orders_clause())
        .group_by(Order.event_id)
    )
    return {
        event_id: (int(orders), int(tickets)) for event_id, orders, tickets in session.execute(stmt)
    }


def gross_kzt(session: Session, event_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """event id -> succeeded ticket payments. The activation fee is not ticket revenue."""
    ids = list(event_ids)
    if not ids:
        return {}
    stmt = (
        select(Order.event_id, func.sum(Payment.amount_kzt))
        .select_from(Payment)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Payment.purpose == PaymentPurpose.TICKET_ORDER,
            Payment.status == PaymentStatus.SUCCEEDED,
            Order.event_id.in_(ids),
        )
        .group_by(Order.event_id)
    )
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def refunds_kzt(session: Session, event_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """event id -> succeeded refunds of those ticket payments."""
    ids = list(event_ids)
    if not ids:
        return {}
    stmt = (
        select(Order.event_id, func.sum(Refund.amount_kzt))
        .select_from(Refund)
        .join(Payment, Payment.id == Refund.payment_id)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Payment.purpose == PaymentPurpose.TICKET_ORDER,
            Payment.status == PaymentStatus.SUCCEEDED,
            Refund.status == RefundStatus.SUCCEEDED,
            Order.event_id.in_(ids),
        )
        .group_by(Order.event_id)
    )
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def checked_in(session: Session, event_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """event id -> tickets already scanned at the door."""
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
    return {event_id: int(total) for event_id, total in session.execute(stmt)}


def ticket_type_rows(session: Session, event_id: uuid.UUID) -> list[tuple[TicketType, int, int]]:
    """Every ticket type of the event (hidden ones too) with (sold, sales before discounts)."""
    sold = (
        select(
            OrderItem.ticket_type_id.label("ticket_type_id"),
            func.sum(OrderItem.quantity).label("sold"),
            func.sum(OrderItem.quantity * OrderItem.unit_price_kzt).label("sales"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event_id, confirmed_orders_clause())
        .group_by(OrderItem.ticket_type_id)
        .subquery()
    )
    stmt = (
        select(TicketType, func.coalesce(sold.c.sold, 0), func.coalesce(sold.c.sales, 0))
        .outerjoin(sold, sold.c.ticket_type_id == TicketType.id)
        .where(TicketType.event_id == event_id)
        .order_by(TicketType.price_kzt, TicketType.name, TicketType.id)
    )
    return [(tt, int(sold_), int(sales)) for tt, sold_, sales in session.execute(stmt)]


def daily_sales(session: Session, event_id: uuid.UUID) -> list[tuple[date, int, int]]:
    """(day, orders, tickets) per day, the day taken in the venue's time zone.

    The zone comes from the venues column, not from a bound value: the same expression has to
    appear in SELECT and GROUP BY, and two bound copies of it are different to Postgres.
    """
    day = cast(func.timezone(Venue.timezone, Order.created_at), Date)
    stmt = (
        select(day, func.count(func.distinct(Order.id)), func.sum(OrderItem.quantity))
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Event, Event.id == Order.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .where(Order.event_id == event_id, confirmed_orders_clause())
        .group_by(day)
        .order_by(day)
    )
    return [(d, int(orders), int(tickets)) for d, orders, tickets in session.execute(stmt)]
