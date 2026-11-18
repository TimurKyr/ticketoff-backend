import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, InvalidInputError, NotFoundError
from app.modules.audit import service as audit
from app.modules.events import repository
from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import EventBucket, EventCreate, EventUpdate, check_dates
from app.modules.identity.models import OrganizerProfile, User
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import TicketType

_CLOSED = (EventStatus.CANCELLED, EventStatus.COMPLETED)

# Changing these after people hold tickets would change what they bought.
_LOCKED_WITH_ORDERS = ("venue_id", "seating_type")


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """SQLite (used only in local checks) drops tzinfo; Postgres keeps it."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _require_venue(session: Session, venue_id: uuid.UUID) -> None:
    if repository.get_venue(session, venue_id) is None:
        raise InvalidInputError("Venue does not exist")


def _require_open(event: Event) -> None:
    if event.status in _CLOSED:
        raise BusinessRuleError(f"Event is {event.status.value} and can no longer be changed")


def _has_orders(session: Session, event: Event) -> bool:
    return repository.has_orders(session, event.id, _now())


def _lock(session: Session, event: Event) -> None:
    """Take FOR UPDATE on the event row and re-read it.

    Ownership is already checked (require_event_owner). Registration and checkout lock the
    event first too, so a status change here and a new order are serialized: either the
    order sees the new status, or this sees the committed order.
    """
    orders_repository.lock_event(session, event.id)  # populate_existing refreshes `event`


def _check_capacity(session: Session, event: Event, capacity: int | None) -> None:
    """Capacity may not go below the ticket types' total nor below what is already held."""
    if capacity is None or capacity == event.capacity:
        return
    types_total = orders_repository.total_quantity_for_event(session, event.id)
    if capacity < types_total:
        raise BusinessRuleError(
            f"Capacity ({capacity}) is below the total ticket quantity ({types_total})"
        )
    held = orders_repository.held_quantity_for_event(session, event.id, _now())
    if capacity < held:
        raise BusinessRuleError(f"Capacity ({capacity}) is below tickets already sold ({held})")


def _audit_status(session: Session, event: Event, user: User, action_type: str) -> None:
    audit.log(
        session,
        actor_user_id=user.id,
        action_type=action_type,
        entity_type="event",
        entity_id=event.id,
        event_id=event.id,
    )


def _require_not_ended(start_at: datetime, end_at: datetime | None, message: str) -> None:
    if _as_aware(end_at or start_at) < _now():
        raise BusinessRuleError(message)


def create_event(session: Session, organizer: OrganizerProfile, payload: EventCreate) -> Event:
    _require_venue(session, payload.venue_id)
    event = Event(organizer_id=organizer.id, status=EventStatus.DRAFT, **payload.model_dump())
    repository.add_event(session, event)
    session.commit()
    return event


def update_event(session: Session, event: Event, payload: EventUpdate, user: User) -> Event:
    _lock(session, event)
    _require_open(event)
    changes = payload.model_dump(exclude_unset=True)
    if "venue_id" in changes:
        _require_venue(session, changes["venue_id"])
    if "capacity" in changes:
        _check_capacity(session, event, changes["capacity"])

    locked = [
        name
        for name in _LOCKED_WITH_ORDERS
        if name in changes and changes[name] != getattr(event, name)
    ]
    if locked and _has_orders(session, event):
        raise BusinessRuleError(f"{', '.join(locked)} cannot be changed once the event has orders")

    # Re-check date rules against the merged result, not only the sent fields.
    merged = {
        name: changes.get(name, getattr(event, name))
        for name in ("start_at", "end_at", "registration_opens_at", "registration_closes_at")
    }
    try:
        check_dates(**{k: (_as_aware(v) if v else v) for k, v in merged.items()})
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    if event.status == EventStatus.PUBLISHED and ("start_at" in changes or "end_at" in changes):
        _require_not_ended(
            merged["start_at"], merged["end_at"], "A published event cannot be moved into the past"
        )

    old_capacity = event.capacity
    for name, value in changes.items():
        setattr(event, name, value)
    if "capacity" in changes and changes["capacity"] != old_capacity:
        audit.log(
            session,
            actor_user_id=user.id,
            action_type="event.capacity_changed",
            entity_type="event",
            entity_id=event.id,
            event_id=event.id,
            details={"old": old_capacity, "new": changes["capacity"]},  # None = no limit
        )
    session.commit()  # the change and its audit row together
    return event


def duplicate_event(session: Session, event: Event) -> Event:
    """Copy the configuration and the ticket types: no orders, tickets, payments or
    check-ins. Ticket types keep their settings (sales windows too); sold counts are not
    stored on the type, so the copy starts with nothing sold."""
    copy = Event(
        organizer_id=event.organizer_id,
        venue_id=event.venue_id,
        title=event.title,
        description=event.description,
        refund_policy=event.refund_policy,
        category=event.category,
        status=EventStatus.DRAFT,
        visibility=event.visibility,
        seating_type=event.seating_type,
        paid_sales_active=False,
        capacity=event.capacity,
        start_at=event.start_at,
        end_at=event.end_at,
        registration_opens_at=event.registration_opens_at,
        registration_closes_at=event.registration_closes_at,
    )
    repository.add_event(session, copy)
    for ticket_type in orders_repository.list_by_event(session, event.id, include_hidden=True):
        session.add(
            TicketType(
                event_id=copy.id,
                section_id=ticket_type.section_id,
                name=ticket_type.name,
                description=ticket_type.description,
                kind=ticket_type.kind,
                price_kzt=ticket_type.price_kzt,
                quantity_total=ticket_type.quantity_total,
                max_per_order=ticket_type.max_per_order,
                sales_start_at=ticket_type.sales_start_at,
                sales_end_at=ticket_type.sales_end_at,
                is_hidden=ticket_type.is_hidden,
            )
        )
    session.commit()
    return copy


def publish_event(session: Session, event: Event, user: User) -> Event:
    _lock(session, event)
    if event.status != EventStatus.DRAFT:
        raise BusinessRuleError("Only a draft event can be published")
    _require_not_ended(
        event.start_at, event.end_at, "An event that has already ended cannot be published"
    )
    if not orders_repository.list_by_event(session, event.id, include_hidden=False):
        raise BusinessRuleError("Add at least one visible ticket type before publishing")
    event.status = EventStatus.PUBLISHED
    _audit_status(session, event, user, "event.published")
    session.commit()  # the status and its audit row together
    return event


def unpublish_event(session: Session, event: Event, user: User) -> Event:
    _lock(session, event)
    if event.status != EventStatus.PUBLISHED:
        raise BusinessRuleError("Only a published event can be unpublished")
    if _has_orders(session, event):
        # Ticket holders would lose the event page and the organizer could edit it as a draft.
        raise BusinessRuleError("An event with orders cannot be unpublished; cancel it instead")
    event.status = EventStatus.DRAFT
    _audit_status(session, event, user, "event.unpublished")
    session.commit()  # the status and its audit row together
    return event


def mark_cancelled(session: Session, event: Event) -> None:
    """Lock the event and set it cancelled, if it is a draft or published. No commit.

    The one place for the "only draft/published" rule. The caller
    (payments.service.cancel_event_with_orders) cancels and refunds the event's orders in
    the same transaction and commits once. The event lock is taken first, as in every
    other inventory change, so new orders and payments wait and then see the cancel.
    """
    _lock(session, event)
    if event.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
        raise BusinessRuleError(f"A {event.status.value} event cannot be cancelled")
    event.status = EventStatus.CANCELLED


def get_public_event(session: Session, event_id: uuid.UUID) -> Event:
    """Published public/unlisted events only. Everything else is a plain 404,
    so the API never reveals that a private or draft event exists."""
    event = repository.get_event(session, event_id)
    if (
        event is None
        or event.status != EventStatus.PUBLISHED
        or event.visibility == EventVisibility.PRIVATE
    ):
        raise NotFoundError("Event not found")
    return event


def list_venues(session: Session) -> list[Venue]:
    return repository.list_venues(session)


def list_public_events(
    session: Session,
    *,
    q: str | None,
    category: str | None,
    from_date: datetime | None,
    to_date: datetime | None,
    include_past: bool,
    limit: int,
    offset: int,
) -> tuple[list[Event], int]:
    return repository.list_public(
        session,
        now=_now(),
        q=q,
        category=category,
        from_date=from_date,
        to_date=to_date,
        include_past=include_past,
        limit=limit,
        offset=offset,
    )


def list_organizer_events(
    session: Session,
    organizer: OrganizerProfile,
    *,
    bucket: EventBucket | None,
    limit: int,
    offset: int,
) -> tuple[list[Event], int]:
    return repository.list_for_organizer(
        session, organizer_id=organizer.id, bucket=bucket, now=_now(), limit=limit, offset=offset
    )
