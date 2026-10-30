import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.orders import repository
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import TicketType
from app.modules.orders.schemas import (
    PublicTicketTypeOut,
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
