import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.orders import repository
from app.modules.orders.enums import OrderStatus, TicketKind
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.qr import issue_ticket_token
from app.modules.orders.schemas import (
    AttendeeIn,
    OrderOut,
    PublicTicketTypeOut,
    RegistrationCreate,
    TicketOut,
    TicketTypeCreate,
    TicketTypeOut,
    TicketTypeUpdate,
    ticket_type_rule_error,
)

# Orders in these statuses hold inventory; cancelled and refunded ones release it.
INVENTORY_HOLDING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.PAID,
    OrderStatus.FREE_CONFIRMED,
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


def _ensure_event_editable(event: Event) -> None:
    if event.status in CLOSED_EVENT_STATUSES:
        raise BusinessRuleError(f"Ticket types cannot be changed on a {event.status} event")


def _sold(session: Session, ticket_types: Sequence[TicketType]) -> dict[uuid.UUID, int]:
    sold = repository.sold_quantities(
        session, [tt.id for tt in ticket_types], INVENTORY_HOLDING_STATUSES
    )
    return {tt.id: sold.get(tt.id, 0) for tt in ticket_types}


def _available(ticket_type: TicketType, sold: int) -> int:
    return max(0, ticket_type.quantity_total - sold)


def get_availability(session: Session, ticket_types: Sequence[TicketType]) -> dict[uuid.UUID, int]:
    """Remaining tickets per ticket type: quantity_total minus what active orders hold."""
    sold = _sold(session, ticket_types)
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
    event = _get_owned_event(session, event_id, organizer_profile_id)
    _ensure_event_editable(event)

    ticket_type = TicketType(event_id=event.id, **data.model_dump())
    repository.add(session, ticket_type)
    session.commit()
    session.refresh(ticket_type)
    return _to_out(ticket_type, sold=0)


def list_ticket_types(
    session: Session, organizer_profile_id: uuid.UUID, event_id: uuid.UUID
) -> list[TicketTypeOut]:
    """All ticket types of the organizer's event, hidden ones included."""
    _get_owned_event(session, event_id, organizer_profile_id)

    ticket_types = repository.list_by_event(session, event_id, include_hidden=True)
    sold = _sold(session, ticket_types)
    return [_to_out(tt, sold[tt.id]) for tt in ticket_types]


def update_ticket_type(
    session: Session,
    organizer_profile_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    data: TicketTypeUpdate,
) -> TicketTypeOut:
    ticket_type = repository.get_ticket_type(session, ticket_type_id)
    if ticket_type is None:
        raise NotFoundError("Ticket type not found")
    event = _get_owned_event(session, ticket_type.event_id, organizer_profile_id)
    _ensure_event_editable(event)

    changes = data.model_dump(exclude_unset=True)
    sold = _sold(session, [ticket_type])[ticket_type.id]

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

    for field, value in changes.items():
        setattr(ticket_type, field, value)
    session.commit()
    session.refresh(ticket_type)
    return _to_out(ticket_type, sold)


def list_public_ticket_types(
    session: Session, event_id: uuid.UUID, now: datetime | None = None
) -> list[PublicTicketTypeOut]:
    """Visible ticket types of a published event that are on sale right now.

    Sold-out types are included with available = 0. Event visibility is not checked yet.
    """
    event = repository.get_event(session, event_id)
    if event is None or event.status != EventStatus.PUBLISHED:
        raise NotFoundError("Event not found")

    ticket_types = repository.list_by_event(
        session, event_id, include_hidden=False, on_sale_at=now or datetime.now(UTC)
    )
    available = get_availability(session, ticket_types)
    return [
        PublicTicketTypeOut.model_validate({**_columns(tt), "available": available[tt.id]})
        for tt in ticket_types
    ]


# --- free registration ---


def _ensure_registration_open(event: Event, now: datetime) -> None:
    if event.start_at <= now:
        raise BusinessRuleError("Event has already started")
    if event.registration_opens_at is not None and now < event.registration_opens_at:
        raise BusinessRuleError("Registration has not opened yet")
    if event.registration_closes_at is not None and now >= event.registration_closes_at:
        raise BusinessRuleError("Registration is closed")


def _check_ticket_type(ticket_type: TicketType | None, event: Event, now: datetime) -> TicketType:
    # Hidden types answer 404 like missing ones, before the kind check, so they are not revealed.
    if ticket_type is None or ticket_type.event_id != event.id or ticket_type.is_hidden:
        raise NotFoundError("Ticket type not found")
    if ticket_type.kind != TicketKind.FREE:
        raise BusinessRuleError("Use checkout for paid tickets")
    if ticket_type.sales_start_at is not None and now < ticket_type.sales_start_at:
        raise BusinessRuleError(f"Sales for {ticket_type.name} have not started")
    if ticket_type.sales_end_at is not None and now >= ticket_type.sales_end_at:
        raise BusinessRuleError(f"Sales for {ticket_type.name} have ended")
    return ticket_type


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


def _order_out(order: Order) -> OrderOut:
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
        }
    )


def register_free(
    session: Session,
    user: User,
    event_id: uuid.UUID,
    data: RegistrationCreate,
    now: datetime | None = None,
) -> OrderOut:
    """Create a confirmed free order with one attendee and one signed ticket per seat.

    Ticket types are locked (FOR UPDATE) before availability is counted, so concurrent
    registrations for the last tickets are serialized and cannot oversell.
    """
    now = now or datetime.now(UTC)
    event = repository.get_event(session, event_id)
    if event is None or event.status != EventStatus.PUBLISHED:
        raise NotFoundError("Event not found")
    _ensure_registration_open(event, now)

    locked = {
        tt.id: tt
        for tt in repository.lock_ticket_types(
            session, [item.ticket_type_id for item in data.items]
        )
    }
    ticket_types = [
        _check_ticket_type(locked.get(item.ticket_type_id), event, now) for item in data.items
    ]
    for item, ticket_type in zip(data.items, ticket_types, strict=True):
        if ticket_type.max_per_order is not None and item.quantity > ticket_type.max_per_order:
            raise BusinessRuleError(
                f"At most {ticket_type.max_per_order} {ticket_type.name} tickets per order"
            )

    available = get_availability(session, ticket_types)
    for item, ticket_type in zip(data.items, ticket_types, strict=True):
        if item.quantity > available[ticket_type.id]:
            raise BusinessRuleError(f"Not enough tickets left for {ticket_type.name}")

    attendees_in = data.attendees or [
        AttendeeIn(name=user.email, email=user.email) for _ in range(data.total_quantity)
    ]
    order = Order(
        buyer_user_id=user.id,
        event_id=event.id,
        status=OrderStatus.FREE_CONFIRMED,
        total_kzt=0,
    )
    attendees = iter(attendees_in)
    for item_in, ticket_type in zip(data.items, ticket_types, strict=True):
        item = OrderItem(
            ticket_type_id=ticket_type.id,
            quantity=item_in.quantity,
            unit_price_kzt=ticket_type.price_kzt,
        )
        order.items.append(item)
        for _ in range(item_in.quantity):
            attendee_in = next(attendees)
            attendee = Attendee(order=order, name=attendee_in.name, email=attendee_in.email)
            # The id is set here, not on flush, because it goes into the signed token.
            ticket_id = uuid.uuid4()
            item.tickets.append(
                Ticket(
                    id=ticket_id,
                    attendee=attendee,
                    qr_token=issue_ticket_token(ticket_id, event.id),
                )
            )

    repository.add_order(session, order)
    session.commit()  # also releases the ticket type locks
    return _order_out(order)


# --- buyer reads ---
# Someone else's order or ticket answers 404, so its existence is not revealed.


def list_my_orders(session: Session, user: User) -> list[OrderOut]:
    return [_order_out(order) for order in repository.list_orders_for_buyer(session, user.id)]


def get_my_order(session: Session, user: User, order_id: uuid.UUID) -> OrderOut:
    order = repository.get_order_with_details(session, order_id)
    if order is None or order.buyer_user_id != user.id:
        raise NotFoundError("Order not found")
    return _order_out(order)


def get_my_ticket(session: Session, user: User, ticket_id: uuid.UUID) -> TicketOut:
    ticket = repository.get_ticket(session, ticket_id)
    if ticket is None or ticket.order_item.order.buyer_user_id != user.id:
        raise NotFoundError("Ticket not found")
    return _ticket_out(ticket, ticket.order_item.ticket_type_id)
