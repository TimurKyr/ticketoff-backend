import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, String, and_, cast, func, or_, select
from sqlalchemy.engine import Row as ResultRow
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload

from app.modules.admin.platform_settings import get_platform_settings
from app.modules.events.models import Event, Row, Seat
from app.modules.identity.models import User
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.promotions.models import PromoCode, PromoRedemption


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
        stmt = stmt.where(sales_window_clause(on_sale_at))
    stmt = stmt.order_by(TicketType.price_kzt, TicketType.name)
    return list(session.scalars(stmt))


def sales_window_clause(at: datetime) -> ColumnElement[bool]:
    """Ticket types whose sales window (either end may be open) contains `at`."""
    return and_(
        or_(TicketType.sales_start_at.is_(None), TicketType.sales_start_at <= at),
        or_(TicketType.sales_end_at.is_(None), TicketType.sales_end_at > at),
    )


def checkout_hold(session: Session) -> timedelta:
    """How long a pending (unpaid) order holds its tickets (platform settings, BE-26)."""
    return timedelta(minutes=get_platform_settings(session).checkout_hold_minutes)


def holding_orders_clause(now: datetime, hold: timedelta) -> ColumnElement[bool]:
    """Orders that hold inventory at `now`.

    Paid and free-confirmed orders always do. A pending order does only while it is younger
    than `hold`: an abandoned checkout releases its tickets on its own, without a cleanup
    job. Cancelled and refunded orders never do.
    """
    return or_(confirmed_orders_clause(), reserving_orders_clause(now, hold))


def confirmed_orders_clause() -> ColumnElement[bool]:
    """Orders whose tickets are sold for good (paid or free-confirmed)."""
    return Order.status.in_([OrderStatus.PAID, OrderStatus.FREE_CONFIRMED])


def reserving_orders_clause(now: datetime, hold: timedelta) -> ColumnElement[bool]:
    """Pending checkouts still inside their hold. The hold runs from orders.created_at."""
    return and_(Order.status == OrderStatus.PENDING, Order.created_at > now - hold)


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
        .where(
            OrderItem.ticket_type_id.in_(ids), holding_orders_clause(now, checkout_hold(session))
        )
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
        .where(TicketType.event_id == event_id, holding_orders_clause(now, checkout_hold(session)))
    )
    return int(session.scalar(stmt) or 0)


def inventory_rows(session: Session, event_id: uuid.UUID, now: datetime) -> list[ResultRow]:
    """Per ticket type of the event (hidden ones included): quantity_total, sold, reserved
    and ticket counts by status, in one query.

    Quantities and ticket counts are aggregated separately and outer-joined onto
    ticket_types: summing order_items.quantity over a join with tickets would count each
    item once per ticket. Types without any orders get zeros.
    sold + reserved uses the same clauses as sold_quantities (holding_orders_clause).
    """
    hold = checkout_hold(session)
    quantities = (
        select(
            OrderItem.ticket_type_id,
            func.sum(OrderItem.quantity).filter(confirmed_orders_clause()).label("sold"),
            func.sum(OrderItem.quantity)
            .filter(reserving_orders_clause(now, hold))
            .label("reserved"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event_id)
        .group_by(OrderItem.ticket_type_id)
        .subquery()
    )

    def by_status(status: TicketStatus, label: str):
        return func.count(Ticket.id).filter(Ticket.status == status).label(label)

    tickets = (
        select(
            OrderItem.ticket_type_id,
            by_status(TicketStatus.CHECKED_IN, "checked_in"),
            by_status(TicketStatus.REFUNDED, "refunded"),
            by_status(TicketStatus.CANCELLED, "cancelled"),
        )
        .select_from(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event_id)
        .group_by(OrderItem.ticket_type_id)
        .subquery()
    )
    stmt = (
        select(
            TicketType.id.label("ticket_type_id"),
            TicketType.name,
            TicketType.quantity_total,
            func.coalesce(quantities.c.sold, 0).label("sold"),
            func.coalesce(quantities.c.reserved, 0).label("reserved"),
            func.coalesce(tickets.c.checked_in, 0).label("checked_in"),
            func.coalesce(tickets.c.refunded, 0).label("refunded"),
            func.coalesce(tickets.c.cancelled, 0).label("cancelled"),
        )
        .outerjoin(quantities, quantities.c.ticket_type_id == TicketType.id)
        .outerjoin(tickets, tickets.c.ticket_type_id == TicketType.id)
        .where(TicketType.event_id == event_id)
        # Same order as the owner's ticket type list.
        .order_by(TicketType.price_kzt, TicketType.name)
    )
    return list(session.execute(stmt))


def total_quantity_for_event(
    session: Session, event_id: uuid.UUID, exclude_id: uuid.UUID | None = None
) -> int:
    """Sum of quantity_total over the event's ticket types, hidden ones included.

    exclude_id leaves one type out (the one being updated).
    """
    stmt = select(func.coalesce(func.sum(TicketType.quantity_total), 0)).where(
        TicketType.event_id == event_id
    )
    if exclude_id is not None:
        stmt = stmt.where(TicketType.id != exclude_id)
    return int(session.scalar(stmt) or 0)


def add(session: Session, ticket_type: TicketType) -> None:
    session.add(ticket_type)
    session.flush()


# Lock order for anything that changes inventory: the event row first, then the rest
# (its ticket types in id order, or the order being paid/cancelled/refunded and then that
# order's tickets, or, for an event cancel, all of its orders and then all of its tickets,
# each in id order). Every caller (free registration, paid checkout, pay, cancel, refund,
# event cancel) must follow it, so concurrent transactions never wait on each other in a
# cycle. Check-in locks a single ticket row and nothing after it, so it cannot close a
# cycle either.


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


def get_order(session: Session, order_id: uuid.UUID) -> Order | None:
    """An order without any lock (e.g. to find its event before locking)."""
    return session.get(Order, order_id)


def lock_order(session: Session, order_id: uuid.UUID) -> Order | None:
    """SELECT ... FROM orders WHERE id = :id FOR UPDATE, held until commit or rollback.

    Serializes pays and cancels of one order. They lock the order's event first and this
    row second; nothing locks an order before an event, so this cannot form a cycle.
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


def get_ticket_for_pdf(session: Session, ticket_id: uuid.UUID) -> Ticket | None:
    """A ticket with everything the printable PDF shows, loaded in one query.

    All relationships are many-to-one, so joinedload adds no duplicate rows. The seat chain
    is an outer join and stays None for general-admission tickets.
    """
    return session.get(
        Ticket,
        ticket_id,
        options=(
            joinedload(Ticket.attendee),
            joinedload(Ticket.order_item)
            .joinedload(OrderItem.order)
            .joinedload(Order.event)
            .joinedload(Event.venue),
            joinedload(Ticket.order_item).joinedload(OrderItem.ticket_type),
            joinedload(Ticket.seat).joinedload(Seat.row).joinedload(Row.section),
        ),
    )


def lock_order_tickets(session: Session, order_id: uuid.UUID) -> list[Ticket]:
    """SELECT ... FOR UPDATE OF tickets: every ticket of the order, in id order.

    Take it after lock_event and lock_order. Check-in locks the ticket row it scans, so a
    cancel or refund holding these locks is serialized with a scan at the door: the scan
    either waits and then sees the new status, or wins and the cancel sees checked_in.
    """
    stmt = (
        select(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(OrderItem.order_id == order_id)
        .order_by(Ticket.id)
        .with_for_update(of=Ticket)
        # Re-read rows already in the session: a scan may have committed while we waited.
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(stmt))


def lock_event_orders(session: Session, event_id: uuid.UUID) -> list[Order]:
    """SELECT ... FOR UPDATE: every order of the event, in id order (event cancel).

    Take it after lock_event. Pay, cancel and refund lock the event first as well, so they
    wait for the whole cancel and then see the new statuses.
    """
    stmt = (
        select(Order)
        .where(Order.event_id == event_id)
        .order_by(Order.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(stmt))


def lock_event_tickets(session: Session, event_id: uuid.UUID) -> list[Ticket]:
    """SELECT ... FOR UPDATE OF tickets: every ticket of the event, in id order.

    Take it after lock_event_orders. Each ticket comes with its order_item loaded (no lock
    on order_items), so callers can group tickets by order without extra queries. A scan
    at the door locks the ticket row it checks in, so it waits for the cancel and then sees
    the new ticket status.
    """
    stmt = (
        select(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event_id)
        .options(contains_eager(Ticket.order_item))
        .order_by(Ticket.id)
        .with_for_update(of=Ticket)
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(stmt).unique())


# --- the organizer's list of an event's orders (BE-30) ---

# A short order number as the web shows it (the first hex digits of the id), or a longer
# piece of the id with its dashes.
_ORDER_ID_PREFIX = re.compile(r"[0-9a-f][0-9a-f-]{3,35}")


def _order_search(q: str) -> ColumnElement[bool]:
    """Orders where `q` is part of the buyer's email or of a guest's name or email (any case;
    % and _ are literal), or the order id (a full id, or the start of one).

    EXISTS, not joins: an order with several matching guests is still one row.
    """
    buyer_matches = (
        select(User.id)
        .where(User.id == Order.buyer_user_id, User.email.icontains(q, autoescape=True))
        .exists()
    )
    guest_matches = (
        select(Attendee.id)
        .where(
            Attendee.order_id == Order.id,
            or_(
                Attendee.name.icontains(q, autoescape=True),
                Attendee.email.icontains(q, autoescape=True),
            ),
        )
        .exists()
    )
    matches: list[ColumnElement[bool]] = [buyer_matches, guest_matches]
    try:
        matches.append(Order.id == uuid.UUID(q))
    except ValueError:
        prefix = q.lower()
        if _ORDER_ID_PREFIX.fullmatch(prefix):
            # uuid::text is lower-case with dashes, e.g. "16b0b9b4-0b4c-...".
            matches.append(cast(Order.id, String).startswith(prefix, autoescape=True))
    return or_(*matches)


def list_event_orders(
    session: Session,
    event_id: uuid.UUID,
    *,
    q: str | None,
    status: OrderStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[Order], int]:
    """One page of the event's orders, newest first, and the number of all matching orders.

    Loaded with everything the list shows, in a fixed number of queries whatever the page
    size: the buyer (joined), the attendees, the items with their ticket types, and the
    tickets. Promo codes and check-ins come from promo_codes_by_order / check_ins_by_ticket.
    """
    conditions: list[ColumnElement[bool]] = [Order.event_id == event_id]
    if status is not None:
        conditions.append(Order.status == status)
    if q:
        conditions.append(_order_search(q))

    total = session.scalar(select(func.count()).select_from(Order).where(*conditions)) or 0
    stmt = (
        select(Order)
        .where(*conditions)
        .options(
            joinedload(Order.buyer),
            selectinload(Order.attendees),
            selectinload(Order.items).options(
                joinedload(OrderItem.ticket_type), selectinload(OrderItem.tickets)
            ),
        )
        # Newest first; id breaks ties (orders created in one transaction share now()).
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), total


def promo_codes_by_order(session: Session, order_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The promo code used by each of these orders (orders without one are missing)."""
    ids = list(order_ids)
    if not ids:
        return {}
    stmt = (
        select(PromoRedemption.order_id, PromoCode.code)
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .where(PromoRedemption.order_id.in_(ids))
    )
    return {order_id: code for order_id, code in session.execute(stmt)}


def check_ins_by_ticket(
    session: Session, ticket_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """When each of these tickets was admitted: its active (not reversed) scan, at most one
    per ticket (uq_check_in_records_active_ticket). Tickets never admitted are missing."""
    ids = list(ticket_ids)
    if not ids:
        return {}
    stmt = select(CheckInRecord.ticket_id, CheckInRecord.checked_in_at).where(
        CheckInRecord.ticket_id.in_(ids), CheckInRecord.is_reversed.is_(False)
    )
    return {ticket_id: at for ticket_id, at in session.execute(stmt)}
