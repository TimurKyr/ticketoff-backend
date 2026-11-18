import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.audit import service as audit
from app.modules.events import service as events_service
from app.modules.events.enums import EventStatus, SeatingType
from app.modules.events.models import Event, Seat
from app.modules.identity.models import User
from app.modules.orders import repository
from app.modules.orders.enums import OrderStatus, TicketKind, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.pdf import TicketPdfData, render_ticket_pdf
from app.modules.orders.qr import issue_ticket_token
from app.modules.orders.schemas import (
    CheckoutCreate,
    EventInventoryOut,
    InventoryTicketTypeOut,
    OrderOut,
    PublicTicketTypeOut,
    RegistrationCreate,
    RegistrationItemIn,
    TicketOut,
    TicketTypeCreate,
    TicketTypeOut,
    TicketTypeUpdate,
    ticket_type_rule_error,
)

# Organizers cannot change ticket types once the event is over or called off.
CLOSED_EVENT_STATUSES = (EventStatus.CANCELLED, EventStatus.COMPLETED)

_RULE_FIELDS = (
    "kind",
    "price_kzt",
    "quantity_total",
    "max_per_order",
    "sales_start_at",
    "sales_end_at",
)


def _get_owned_event(
    session: Session, event_id: uuid.UUID, organizer_profile_id: uuid.UUID
) -> Event:
    event = repository.get_event(session, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    if event.organizer_id != organizer_profile_id:
        raise PermissionDeniedError("Event does not belong to this organizer")
    return event


def _lock_owned_event(
    session: Session, event_id: uuid.UUID, organizer_profile_id: uuid.UUID
) -> Event:
    """The organizer's event, locked FOR UPDATE until commit.

    Ownership is checked before the lock is taken. Holding the event lock serializes ticket
    type changes with each other and with registrations, which lock the event first too, so
    capacity sums and sold counts cannot change underneath us.
    """
    event = _get_owned_event(session, event_id, organizer_profile_id)
    repository.lock_event(session, event_id)  # populate_existing refreshes `event`
    return event


def _check_capacity(event: Event, current_total: int, new_total: int) -> None:
    """The sum of quantity_total (hidden types included) must fit the event's capacity.

    A change that does not raise the sum is always allowed, so an event already over capacity
    (rows from before update_event checked capacity) can still be brought back down.
    """
    if event.capacity is None or new_total <= current_total:
        return
    if new_total > event.capacity:
        raise BusinessRuleError(
            f"Total ticket quantity ({new_total}) exceeds event capacity ({event.capacity})"
        )


def _ensure_event_editable(event: Event) -> None:
    if event.status in CLOSED_EVENT_STATUSES:
        raise BusinessRuleError(f"Ticket types cannot be changed on a {event.status} event")


def _sold(
    session: Session, ticket_types: Sequence[TicketType], now: datetime | None = None
) -> dict[uuid.UUID, int]:
    sold = repository.sold_quantities(
        session, [tt.id for tt in ticket_types], now or datetime.now(UTC)
    )
    return {tt.id: sold.get(tt.id, 0) for tt in ticket_types}


def _available(ticket_type: TicketType, sold: int) -> int:
    return max(0, ticket_type.quantity_total - sold)


def get_availability(
    session: Session, ticket_types: Sequence[TicketType], now: datetime | None = None
) -> dict[uuid.UUID, int]:
    """Remaining tickets per ticket type: quantity_total minus what holding orders hold.

    Pending orders older than the checkout hold no longer count (see holding_orders_clause).
    """
    sold = _sold(session, ticket_types, now)
    return {tt.id: _available(tt, sold[tt.id]) for tt in ticket_types}


def _columns(ticket_type: TicketType) -> dict[str, object]:
    return {c.key: getattr(ticket_type, c.key) for c in TicketType.__table__.columns}


def _to_out(ticket_type: TicketType, sold: int) -> TicketTypeOut:
    return TicketTypeOut.model_validate(
        {**_columns(ticket_type), "sold": sold, "available": _available(ticket_type, sold)}
    )


def create_ticket_type(
    session: Session,
    organizer_profile_id: uuid.UUID,
    event_id: uuid.UUID,
    data: TicketTypeCreate,
) -> TicketTypeOut:
    event = _lock_owned_event(session, event_id, organizer_profile_id)
    _ensure_event_editable(event)
    current_total = repository.total_quantity_for_event(session, event.id)
    _check_capacity(event, current_total, current_total + data.quantity_total)

    ticket_type = TicketType(event_id=event.id, **data.model_dump())
    repository.add(session, ticket_type)
    session.commit()
    session.refresh(ticket_type)
    return _to_out(ticket_type, sold=0)


def get_inventory(session: Session, event: Event, now: datetime | None = None) -> EventInventoryOut:
    """Inventory of the organizer's event (ownership is checked by the route).

    Order-based, like checkout: sold + reserved per type is exactly TicketTypeOut.sold, and
    available is exactly TicketTypeOut.available, so the two endpoints always agree.
    Ticket statuses (checked_in, refunded, cancelled) are extra counts from tickets.
    """
    now = now or datetime.now(UTC)  # one clock reading for every number below
    types = [
        InventoryTicketTypeOut(
            ticket_type_id=row.ticket_type_id,
            name=row.name,
            quantity_total=row.quantity_total,
            sold=row.sold,
            reserved=row.reserved,
            available=max(0, row.quantity_total - row.sold - row.reserved),
            checked_in=row.checked_in,
            refunded=row.refunded,
            cancelled=row.cancelled,
        )
        for row in repository.inventory_rows(session, event.id, now)
    ]
    sold = sum(t.sold for t in types)
    reserved = sum(t.reserved for t in types)
    available = sum(t.available for t in types)
    if event.capacity is not None:
        # Checkout also refuses orders beyond capacity - held, whatever the types allow.
        available = min(available, max(0, event.capacity - (sold + reserved)))
    return EventInventoryOut(
        event_id=event.id,
        capacity=event.capacity,
        sold=sold,
        reserved=reserved,
        available=available,
        checked_in=sum(t.checked_in for t in types),
        refunded=sum(t.refunded for t in types),
        cancelled=sum(t.cancelled for t in types),
        ticket_types=types,
    )


def list_ticket_types(
    session: Session,
    organizer_profile_id: uuid.UUID,
    event_id: uuid.UUID,
    now: datetime | None = None,
) -> list[TicketTypeOut]:
    """All ticket types of the organizer's event, hidden ones included."""
    _get_owned_event(session, event_id, organizer_profile_id)

    ticket_types = repository.list_by_event(session, event_id, include_hidden=True)
    sold = _sold(session, ticket_types, now or datetime.now(UTC))
    return [_to_out(tt, sold[tt.id]) for tt in ticket_types]


_AUDITED_TYPE_FIELDS = (
    ("price_kzt", "ticket_type.price_changed"),
    ("quantity_total", "ticket_type.quantity_changed"),
)


def update_ticket_type(
    session: Session,
    organizer_profile_id: uuid.UUID,
    event_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    data: TicketTypeUpdate,
    now: datetime | None = None,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> TicketTypeOut:
    """actor_user_id is recorded on the audit rows; the route always passes the caller.
    None means a system or script change (audit rows allow a missing actor)."""
    # Ownership comes from the event in the path; the type is loaded after the lock, so it
    # reflects any change committed by whoever held the lock before us.
    event = _lock_owned_event(session, event_id, organizer_profile_id)
    # Read the clock under the lock: `sold` drops expired pending orders, and a stale time
    # taken before waiting for the lock would count differently from the order that won it.
    now = now or datetime.now(UTC)
    ticket_type = repository.get_ticket_type(session, ticket_type_id)
    # A type of another event is "not found" here, even if the organizer owns that event too.
    if ticket_type is None or ticket_type.event_id != event.id:
        raise NotFoundError("Ticket type not found")
    _ensure_event_editable(event)

    changes = data.model_dump(exclude_unset=True)
    sold = _sold(session, [ticket_type], now)[ticket_type.id]

    if "kind" in changes and changes["kind"] != ticket_type.kind and sold > 0:
        raise BusinessRuleError("Cannot change kind after tickets have been sold")

    merged = {field: changes.get(field, getattr(ticket_type, field)) for field in _RULE_FIELDS}
    error = ticket_type_rule_error(**merged)
    if error:
        raise BusinessRuleError(error)
    if merged["quantity_total"] < sold:
        raise BusinessRuleError(
            f"quantity_total cannot be lower than tickets already sold ({sold})"
        )
    if "quantity_total" in changes:
        others = repository.total_quantity_for_event(session, event.id, exclude_id=ticket_type.id)
        _check_capacity(
            event, others + ticket_type.quantity_total, others + merged["quantity_total"]
        )

    old = {"price_kzt": ticket_type.price_kzt, "quantity_total": ticket_type.quantity_total}
    for field, value in changes.items():
        setattr(ticket_type, field, value)
    # One row per changed value (two if both change), only when it really changed.
    for field, action_type in _AUDITED_TYPE_FIELDS:
        if field in changes and changes[field] != old[field]:
            audit.log(
                session,
                actor_user_id=actor_user_id,
                action_type=action_type,
                entity_type="ticket_type",
                entity_id=ticket_type.id,
                event_id=event.id,
                details={"name": ticket_type.name, "old": old[field], "new": changes[field]},
            )
    session.commit()  # the change and its audit rows together
    session.refresh(ticket_type)
    return _to_out(ticket_type, sold)


def list_public_ticket_types(
    session: Session, event_id: uuid.UUID, now: datetime | None = None
) -> list[PublicTicketTypeOut]:
    """Visible ticket types of a published public or unlisted event, on sale right now.

    Sold-out types are included with available = 0. Draft, cancelled, completed and private
    events answer 404, the same as the public event page and registration.
    """
    events_service.get_public_event(session, event_id)

    now = now or datetime.now(UTC)
    ticket_types = repository.list_by_event(session, event_id, include_hidden=False, on_sale_at=now)
    available = get_availability(session, ticket_types, now)
    return [
        PublicTicketTypeOut.model_validate({**_columns(tt), "available": available[tt.id]})
        for tt in ticket_types
    ]


# --- ordering: shared by free registration and paid checkout ---

# What to tell a buyer who sent a ticket type of the other kind.
_WRONG_KIND_MESSAGE = {
    TicketKind.FREE: "Use checkout for paid tickets",
    TicketKind.PAID: "Use registration for free tickets",
}


def _ensure_registration_open(event: Event, now: datetime) -> None:
    if event.start_at <= now:
        raise BusinessRuleError("Event has already started")
    if event.registration_opens_at is not None and now < event.registration_opens_at:
        raise BusinessRuleError("Registration has not opened yet")
    if event.registration_closes_at is not None and now >= event.registration_closes_at:
        raise BusinessRuleError("Registration is closed")


def _check_ticket_type(
    ticket_type: TicketType | None, event: Event, now: datetime, kind: TicketKind
) -> TicketType:
    # Hidden types answer 404 like missing ones, before the kind check, so they are not revealed.
    if ticket_type is None or ticket_type.event_id != event.id or ticket_type.is_hidden:
        raise NotFoundError("Ticket type not found")
    if ticket_type.kind != kind:
        raise BusinessRuleError(_WRONG_KIND_MESSAGE[kind])
    if ticket_type.sales_start_at is not None and now < ticket_type.sales_start_at:
        raise BusinessRuleError(f"Sales for {ticket_type.name} have not started")
    if ticket_type.sales_end_at is not None and now >= ticket_type.sales_end_at:
        raise BusinessRuleError(f"Sales for {ticket_type.name} have ended")
    return ticket_type


def _lock_and_validate(
    session: Session,
    event_id: uuid.UUID,
    items: Sequence[RegistrationItemIn],
    now: datetime,
    kind: TicketKind,
) -> tuple[Event, list[TicketType]]:
    """Lock the event and the requested ticket types, then check every rule for a new order.

    Returns the event and the ticket types in the same order as `items`. The locks are
    held until the caller commits or rolls back.

    Lock order (every inventory change must follow it, so transactions never wait on each
    other in a cycle): the event row first, then its ticket types in id order, nothing else.
    Counting happens only after both locks, so concurrent orders are serialized: they cannot
    oversell a ticket type or the event capacity, and cannot slip in after a concurrent cancel.
    """
    events_service.get_public_event(session, event_id)  # 404 before taking any lock
    repository.lock_event(session, event_id)
    # Same check again on the locked, freshly re-read row: a cancel (or switch to private)
    # that committed while we waited for the lock wins.
    event = events_service.get_public_event(session, event_id)
    if event.seating_type == SeatingType.ASSIGNED:
        raise BusinessRuleError("This event uses seat selection")
    _ensure_registration_open(event, now)
    # Before the ticket types: an inactive event always gives this error, even if a type
    # is also sold out or off sale.
    if kind == TicketKind.PAID and not event.paid_sales_active:
        raise BusinessRuleError("Paid ticket sales are not active for this event")

    locked = {
        tt.id: tt
        for tt in repository.lock_ticket_types(session, [item.ticket_type_id for item in items])
    }
    ticket_types = [
        _check_ticket_type(locked.get(item.ticket_type_id), event, now, kind) for item in items
    ]
    for item, ticket_type in zip(items, ticket_types, strict=True):
        if ticket_type.max_per_order is not None and item.quantity > ticket_type.max_per_order:
            raise BusinessRuleError(
                f"At most {ticket_type.max_per_order} {ticket_type.name} tickets per order"
            )

    available = get_availability(session, ticket_types, now)
    for item, ticket_type in zip(items, ticket_types, strict=True):
        if item.quantity > available[ticket_type.id]:
            raise BusinessRuleError(f"Not enough tickets left for {ticket_type.name}")
    if event.capacity is not None:
        held = repository.held_quantity_for_event(session, event.id, now)
        if held + sum(item.quantity for item in items) > event.capacity:
            raise BusinessRuleError("Not enough capacity left")

    return event, ticket_types


def _compute_total(
    items: Sequence[RegistrationItemIn], ticket_types: Sequence[TicketType]
) -> tuple[int, int]:
    """(total_kzt, discount_kzt) from the locked DB prices; the client never sends a price."""
    total = sum(
        item.quantity * ticket_type.price_kzt
        for item, ticket_type in zip(items, ticket_types, strict=True)
    )
    discount = 0  # BE-16: apply promo here
    return total, discount


def issue_tickets(order: Order) -> None:
    """One signed ticket per attendee, attached to the attendee's order item.

    Goes through relationships, not raw ids: on a new order nothing is flushed yet, and
    order_out reads the tickets from item.tickets.
    """
    for attendee in order.attendees:
        # The id is set here, not on flush, because it goes into the signed token.
        ticket_id = uuid.uuid4()
        attendee.order_item.tickets.append(
            Ticket(id=ticket_id, attendee=attendee, qr_token=issue_ticket_token(ticket_id))
        )


def _ticket_out(ticket: Ticket, ticket_type_id: uuid.UUID) -> TicketOut:
    return TicketOut.model_validate(
        {
            "id": ticket.id,
            "ticket_type_id": ticket_type_id,
            "attendee": {"name": ticket.attendee.name, "email": ticket.attendee.email},
            "status": ticket.status,
            "qr_token": ticket.qr_token,
        }
    )


def order_out(order: Order) -> OrderOut:
    # Sorted by id so a freshly created order and a reloaded one list rows the same way.
    items = sorted(order.items, key=lambda item: item.id)
    tickets = sorted(
        ((item, ticket) for item in items for ticket in item.tickets),
        key=lambda pair: pair[1].id,
    )
    return OrderOut.model_validate(
        {
            "id": order.id,
            "event_id": order.event_id,
            "status": order.status,
            "total_kzt": order.total_kzt,
            "items": [
                {
                    "ticket_type_id": item.ticket_type_id,
                    "quantity": item.quantity,
                    "unit_price_kzt": item.unit_price_kzt,
                }
                for item in items
            ],
            "tickets": [_ticket_out(ticket, item.ticket_type_id) for item, ticket in tickets],
            "expires_at": _expires_at(order),
        }
    )


def _expires_at(order: Order) -> datetime | None:
    """When a pending order stops holding its tickets; None for every other status."""
    if order.status != OrderStatus.PENDING:
        return None
    return order.created_at + repository.checkout_hold()


def _build_order(
    user: User,
    event: Event,
    data: RegistrationCreate,
    ticket_types: Sequence[TicketType],
    status: OrderStatus,
) -> Order:
    """A new order with its items and one attendee per ticket, linked to the attendee's item.

    Prices come from the locked ticket types, never from the request.
    """
    total, discount = _compute_total(data.items, ticket_types)
    order = Order(
        buyer_user_id=user.id,
        event_id=event.id,
        status=status,
        total_kzt=total,
        discount_kzt=discount,
    )
    # Attendees are matched to tickets in item order.
    attendees = iter(data.attendees)
    for item_in, ticket_type in zip(data.items, ticket_types, strict=True):
        item = OrderItem(
            ticket_type_id=ticket_type.id,
            quantity=item_in.quantity,
            unit_price_kzt=ticket_type.price_kzt,
        )
        order.items.append(item)
        for _ in range(item_in.quantity):
            attendee_in = next(attendees)
            order.attendees.append(
                Attendee(order_item=item, name=attendee_in.name, email=attendee_in.email)
            )
    return order


def register_free(
    session: Session,
    user: User,
    event_id: uuid.UUID,
    data: RegistrationCreate,
    now: datetime | None = None,
) -> OrderOut:
    """Create a confirmed free order with one attendee and one signed ticket per seat.

    Locking and every check are in _lock_and_validate.
    """
    now = now or datetime.now(UTC)
    event, ticket_types = _lock_and_validate(session, event_id, data.items, now, TicketKind.FREE)
    order = _build_order(user, event, data, ticket_types, OrderStatus.FREE_CONFIRMED)
    issue_tickets(order)

    repository.add_order(session, order)
    session.commit()  # also releases the ticket type locks
    return order_out(order)


def checkout(
    session: Session,
    user: User,
    event_id: uuid.UUID,
    data: CheckoutCreate,
    now: datetime | None = None,
) -> OrderOut:
    """Create a pending order for paid tickets and hold the inventory for the checkout hold.

    No tickets yet: they are issued only when the (simulated) payment succeeds. Until then
    the order counts against stock; once it is older than the hold it stops counting.
    Locking and every check (including paid_sales_active) are in _lock_and_validate.
    """
    now = now or datetime.now(UTC)
    event, ticket_types = _lock_and_validate(session, event_id, data.items, now, TicketKind.PAID)
    order = _build_order(user, event, data, ticket_types, OrderStatus.PENDING)

    repository.add_order(session, order)
    # created_at is the DB's now(); load it so expires_at can be computed.
    session.refresh(order, ["created_at"])
    session.commit()  # also releases the ticket type locks
    return order_out(order)


# --- cancellation (BE-19) ---

_ALREADY_CLOSED = {
    OrderStatus.CANCELLED: "Order is already cancelled",
    OrderStatus.REFUNDED: "Order is already refunded",
}


def _canceller(order: Order, user: User) -> str | None:
    """'buyer', 'organizer' (owner of the order's event), or None for anyone else.

    Buyer and event owner never change, so the unlocked read is enough to decide this.
    """
    if order.buyer_user_id == user.id:
        return "buyer"
    if order.event.organizer.user_id == user.id:
        return "organizer"
    return None


def cancel_order(
    session: Session, user: User, order_id: uuid.UUID, now: datetime | None = None
) -> OrderOut:
    """Cancel a free registration (buyer or event owner) or the buyer's own pending checkout.

    Moving the order to cancelled releases its tickets for sale (inventory counts orders,
    not tickets). Paid orders are refunded instead (refund_order). Anyone who is neither
    the buyer nor the event's owner gets 404, so the order's existence is not revealed.

    Locks: event, then order, then the order's tickets (repository lock order). The ticket
    locks serialize this with a scan at the door, which locks the ticket row it checks in.
    """
    # Unlocked read: only to find the event to lock and to answer 404 early.
    order = repository.get_order(session, order_id)
    by = _canceller(order, user) if order is not None else None
    if by is None:
        raise NotFoundError("Order not found")
    event = repository.lock_event(session, order.event_id)
    order = repository.lock_order(session, order_id)
    tickets = repository.lock_order_tickets(session, order.id)
    now = now or datetime.now(UTC)  # after the locks, like pay

    if order.status in _ALREADY_CLOSED:
        raise BusinessRuleError(_ALREADY_CLOSED[order.status])
    if order.status == OrderStatus.PAID:
        raise BusinessRuleError("Paid orders are refunded by the organizer")
    if order.status == OrderStatus.PENDING and by != "buyer":
        raise BusinessRuleError("Only the buyer can cancel a pending checkout")
    if now >= event.start_at:
        raise BusinessRuleError("Event has already started")
    if any(ticket.status == TicketStatus.CHECKED_IN for ticket in tickets):
        raise BusinessRuleError("A ticket of this order was already used")

    order.status = OrderStatus.CANCELLED
    for ticket in tickets:  # all valid here: checked_in was refused above
        ticket.status = TicketStatus.CANCELLED
    audit.log(
        session,
        actor_user_id=user.id,
        action_type="order.cancelled",
        entity_type="order",
        entity_id=order.id,
        event_id=order.event_id,
        details={"by": by, "ticket_ids": [str(ticket.id) for ticket in tickets]},
    )
    session.commit()  # order, tickets and audit row together
    return order_out(repository.get_order_with_details(session, order.id, reload=True))


# --- buyer reads ---
# Someone else's order or ticket answers 404, so its existence is not revealed.


def list_my_orders(session: Session, user: User) -> list[OrderOut]:
    return [order_out(order) for order in repository.list_orders_for_buyer(session, user.id)]


def get_my_order(session: Session, user: User, order_id: uuid.UUID) -> OrderOut:
    order = repository.get_order_with_details(session, order_id)
    if order is None or order.buyer_user_id != user.id:
        raise NotFoundError("Order not found")
    return order_out(order)


def get_my_ticket(session: Session, user: User, ticket_id: uuid.UUID) -> TicketOut:
    ticket = repository.get_ticket(session, ticket_id)
    if ticket is None or ticket.order_item.order.buyer_user_id != user.id:
        raise NotFoundError("Ticket not found")
    return _ticket_out(ticket, ticket.order_item.ticket_type_id)


# --- printable ticket ---

# No printable copy of a ticket that can no longer get anyone in.
_DEAD_TICKET_STATUSES = (TicketStatus.CANCELLED, TicketStatus.REFUNDED)
# Only a settled order's tickets print: not pending (unpaid), cancelled or refunded.
_CONFIRMED_ORDER_STATUSES = (OrderStatus.PAID, OrderStatus.FREE_CONFIRMED)


def _venue_line(event: Event) -> str:
    venue = event.venue
    return ", ".join(part for part in (venue.name, venue.address, venue.city) if part)


def _seat_line(seat: Seat | None) -> str | None:
    if seat is None:
        return None
    return f"{seat.row.section.name} · Row {seat.row.label} · Seat {seat.number}"


def get_my_ticket_pdf(session: Session, user: User, ticket_id: uuid.UUID) -> tuple[bytes, str]:
    """A4 PDF of one of my tickets and its download filename.

    The QR is the ticket's stored qr_token: no new token and no new row, so the printout
    and the phone copy are the same entry. Checked-in tickets and tickets of completed
    events still print; cancelled or refunded tickets, orders that are not paid or
    free_confirmed, and cancelled events do not.
    """
    ticket = repository.get_ticket_for_pdf(session, ticket_id)
    if ticket is None or ticket.order_item.order.buyer_user_id != user.id:
        raise NotFoundError("Ticket not found")
    if ticket.status in _DEAD_TICKET_STATUSES:
        raise BusinessRuleError("Ticket is no longer valid")
    order = ticket.order_item.order
    if order.status not in _CONFIRMED_ORDER_STATUSES:
        raise BusinessRuleError("Order is not confirmed")
    event = order.event
    # cancel_event leaves tickets 'valid', so the event status is checked separately.
    if event.status == EventStatus.CANCELLED:
        raise BusinessRuleError("Event was cancelled")

    # start_at is stored in UTC; a ticket shows the time at the venue.
    tz_name = event.venue.timezone
    data = TicketPdfData(
        event_title=event.title,
        starts_at_local=event.start_at.astimezone(ZoneInfo(tz_name)),
        tz_name=tz_name,
        venue_line=_venue_line(event),
        ticket_type=ticket.order_item.ticket_type.name,
        guest_name=ticket.attendee.name,
        seat_line=_seat_line(ticket.seat),
        ticket_id=ticket.id,
        qr_token=ticket.qr_token,
    )
    return render_ticket_pdf(data), f"ticket-{ticket.id}.pdf"
