import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.modules.events.models import Event
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, OrderItem, Ticket, TicketType


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


def held_quantity_for_event(
    session: Session, event_id: uuid.UUID, order_statuses: Iterable[OrderStatus]
) -> int:
    """Sum of order_items.quantity over all ticket types of the event, for the given statuses."""
    stmt = (
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .join(TicketType, TicketType.id == OrderItem.ticket_type_id)
        .where(TicketType.event_id == event_id, Order.status.in_(list(order_statuses)))
    )
    return int(session.scalar(stmt) or 0)


def add(session: Session, ticket_type: TicketType) -> None:
    session.add(ticket_type)
    session.flush()


# Lock order for anything that changes inventory: the event row first, then its ticket
# types in id order. Every caller (free registration, paid checkout) must follow it, so
# concurrent transactions never wait on each other in a cycle.


def lock_event(session: Session, event_id: uuid.UUID) -> Event | None:
    """SELECT ... FROM events WHERE id = :id FOR UPDATE, held until commit or rollback."""
    stmt = (
        select(Event)
        .where(Event.id == event_id)
        .with_for_update()
        # Reload the row if it is already in the session: a concurrent cancel may have
        # committed while we waited for the lock.
        .execution_options(populate_existing=True)
    )
    return session.scalar(stmt)


def lock_ticket_types(session: Session, ticket_type_ids: Iterable[uuid.UUID]) -> list[TicketType]:
    """SELECT ... FOR UPDATE on the given ticket types, held until commit or rollback.

    Rows are locked in id order so concurrent orders always lock in the same order and
    cannot deadlock. Lock the event first (see above), and call this before counting
    sold tickets. Unknown ids are skipped.
    """
    ids = sorted(set(ticket_type_ids))
    if not ids:
        return []
    stmt = (
        select(TicketType)
        .where(TicketType.id.in_(ids))
        .order_by(TicketType.id)
        .with_for_update()
        # Reload rows that are already in the session: other transactions may have
        # changed them before we got the lock.
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(stmt))


def add_order(session: Session, order: Order) -> None:
    """Insert an order with everything attached to it (items, tickets, attendees)."""
    session.add(order)
    session.flush()


# Items, their tickets and each ticket's attendee: everything OrderOut needs.
_ORDER_DETAILS = (
    selectinload(Order.items).selectinload(OrderItem.tickets).joinedload(Ticket.attendee)
)


def get_order_with_details(session: Session, order_id: uuid.UUID) -> Order | None:
    return session.get(Order, order_id, options=[_ORDER_DETAILS])


def list_orders_for_buyer(session: Session, buyer_user_id: uuid.UUID) -> list[Order]:
    # Newest first; id breaks ties (orders created in one transaction share now()).
    stmt = (
        select(Order)
        .where(Order.buyer_user_id == buyer_user_id)
        .options(_ORDER_DETAILS)
        .order_by(Order.created_at.desc(), Order.id)
    )
    return list(session.scalars(stmt))


def get_ticket(session: Session, ticket_id: uuid.UUID) -> Ticket | None:
    """A ticket with its attendee and order item, plus the order (for the buyer check)."""
    return session.get(
        Ticket,
        ticket_id,
        options=(
            joinedload(Ticket.attendee),
            joinedload(Ticket.order_item).joinedload(OrderItem.order),
        ),
    )
