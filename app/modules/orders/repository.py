import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.config import get_settings
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


def checkout_hold() -> timedelta:
    """How long a pending (unpaid) order holds its tickets."""
    return timedelta(minutes=get_settings().checkout_hold_minutes)


def holding_orders_clause(now: datetime, hold: timedelta) -> ColumnElement[bool]:
    """Orders that hold inventory at `now`.

    Paid and free-confirmed orders always do. A pending order does only while it is younger
    than `hold`: an abandoned checkout releases its tickets on its own, without a cleanup
    job. Cancelled and refunded orders never do.
    """
    return or_(
        Order.status.in_([OrderStatus.PAID, OrderStatus.FREE_CONFIRMED]),
        and_(Order.status == OrderStatus.PENDING, Order.created_at > now - hold),
    )


def sold_quantities(
    session: Session, ticket_type_ids: Iterable[uuid.UUID], now: datetime
) -> dict[uuid.UUID, int]:
    """Sum of order_items.quantity per ticket type, over orders that hold inventory at `now`.

    Ticket types with no matching items are absent from the result.
    """
    ids = list(ticket_type_ids)
    if not ids:
        return {}
    stmt = (
        select(OrderItem.ticket_type_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.ticket_type_id.in_(ids), holding_orders_clause(now, checkout_hold()))
        .group_by(OrderItem.ticket_type_id)
    )
    return {ticket_type_id: int(total) for ticket_type_id, total in session.execute(stmt)}


def held_quantity_for_event(session: Session, event_id: uuid.UUID, now: datetime) -> int:
    """Sum of order_items.quantity over all ticket types of the event, over orders that hold
    inventory at `now`."""
    stmt = (
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .join(TicketType, TicketType.id == OrderItem.ticket_type_id)
        .where(TicketType.event_id == event_id, holding_orders_clause(now, checkout_hold()))
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


def get_order_with_details(
    session: Session, order_id: uuid.UUID, *, reload: bool = False
) -> Order | None:
    """With reload, query the database even if the order is already in the session and
    overwrite what is loaded (items and tickets included)."""
    return session.get(Order, order_id, options=[_ORDER_DETAILS], populate_existing=reload)


def lock_order(session: Session, order_id: uuid.UUID) -> Order | None:
    """SELECT ... FROM orders WHERE id = :id FOR UPDATE, held until commit or rollback.

    Serializes payments of one order. Pay takes only this lock (never the event or ticket
    type locks), so it cannot form a cycle with checkout or registration.
    """
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        # Reload if already in the session: a concurrent pay may have committed while we
        # waited for the lock.
        .execution_options(populate_existing=True)
    )
    return session.scalar(stmt)


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
