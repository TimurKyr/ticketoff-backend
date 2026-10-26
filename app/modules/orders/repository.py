import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.events.models import Event
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, OrderItem, TicketType


def get_event(session: Session, event_id: uuid.UUID) -> Event | None:
    return session.get(Event, event_id)


def get_ticket_type(session: Session, ticket_type_id: uuid.UUID) -> TicketType | None:
    return session.get(TicketType, ticket_type_id)


def list_by_event(
    session: Session,
    event_id: uuid.UUID,
    *,
    include_hidden: bool,
    on_sale_at: datetime | None = None,
) -> list[TicketType]:
    """Ticket types of an event; with on_sale_at, only those whose sales window contains it."""
    stmt = select(TicketType).where(TicketType.event_id == event_id)
    if not include_hidden:
        stmt = stmt.where(TicketType.is_hidden.is_(False))
    if on_sale_at is not None:
        stmt = stmt.where(
            or_(TicketType.sales_start_at.is_(None), TicketType.sales_start_at <= on_sale_at),
            or_(TicketType.sales_end_at.is_(None), TicketType.sales_end_at > on_sale_at),
        )
    stmt = stmt.order_by(TicketType.price_kzt, TicketType.name)
    return list(session.scalars(stmt))


def sold_quantities(
    session: Session,
    ticket_type_ids: Iterable[uuid.UUID],
    order_statuses: Iterable[OrderStatus],
) -> dict[uuid.UUID, int]:
    """Sum of order_items.quantity per ticket type, for orders in the given statuses.

    Ticket types with no matching items are absent from the result.
    """
    ids = list(ticket_type_ids)
    if not ids:
        return {}
    stmt = (
        select(OrderItem.ticket_type_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.ticket_type_id.in_(ids), Order.status.in_(list(order_statuses)))
        .group_by(OrderItem.ticket_type_id)
    )
    return {ticket_type_id: int(total) for ticket_type_id, total in session.execute(stmt)}


def add(session: Session, ticket_type: TicketType) -> None:
    session.add(ticket_type)
    session.flush()
