import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import (
    CAPACITY_BELOW_SOLD,
    CAPACITY_BELOW_TICKET_QUANTITY,
    BusinessRuleError,
    InvalidInputError,
    NotFoundError,
)
from app.modules.audit import repository as audit_repository
from app.modules.audit import service as audit
from app.modules.events import calendar, repository, slugs
from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import (
    EventBucket,
    EventCreate,
    EventUpdate,
    availability_of,
    check_dates,
)
from app.modules.identity.models import OrganizerProfile, User
from app.modules.notifications import flows as notification_flows
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
            f"Capacity ({capacity}) is below the total ticket quantity ({types_total})",
            code=CAPACITY_BELOW_TICKET_QUANTITY,
        )
    held = orders_repository.held_quantity_for_event(session, event.id, _now())
    if capacity < held:
        raise BusinessRuleError(
            f"Capacity ({capacity}) is below tickets already sold ({held})",
            code=CAPACITY_BELOW_SOLD,
        )


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


def _sync_slug(session: Session, event: Event) -> None:
    """Private events have no slug (their UUID link cannot be guessed); any other event
    keeps the one it has, or gets one from its current title. Never renamed on a title
    edit, so links already shared keep working."""
    if event.visibility == EventVisibility.PRIVATE:
        event.slug = None
    elif event.slug is None:
        event.slug = slugs.unique_slug(session, event.title)


def create_event(session: Session, organizer: OrganizerProfile, payload: EventCreate) -> Event:
    _require_venue(session, payload.venue_id)
    event = Event(organizer_id=organizer.id, status=EventStatus.DRAFT, **payload.model_dump())
    _sync_slug(session, event)
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
    before = notification_flows.schedule_of(event)
    for name, value in changes.items():
        setattr(event, name, value)
    if "visibility" in changes:
        _sync_slug(session, event)
    # Ticket holders hear about a new time or venue of a published event (after the commit).
    notification_flows.event_updated(session, event, before)
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
    _sync_slug(session, copy)  # its own slug: the original keeps its link
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


def get_public_event(session: Session, key: uuid.UUID | str) -> Event:
    """Published public/unlisted events only, by id or by slug (BE-35). Everything else is
    a plain 404, so the API never reveals that a private or draft event exists."""
    if isinstance(key, str) and slugs.is_uuid(key):
        key = uuid.UUID(key)
    if isinstance(key, uuid.UUID):
        event = repository.get_event(session, key)
    else:
        event = repository.get_event_by_slug(session, key)
    if (
        event is None
        or event.status != EventStatus.PUBLISHED
        or event.visibility == EventVisibility.PRIVATE
    ):
        raise NotFoundError("Event not found")
    return event


def get_calendar_event(session: Session, event_id: uuid.UUID) -> Event:
    """GET /events/{id}/calendar.ics: what get_public_event allows, plus cancelled and
    completed events that were published once (non-private).

    A cancelled event's file is how a calendar learns of the cancel (STATUS:CANCELLED, the
    same UID). An event cancelled while still a draft was never public: 404, as for drafts.
    """
    event = repository.get_event(session, event_id)
    if event is None or event.visibility == EventVisibility.PRIVATE:
        raise NotFoundError("Event not found")
    if event.status == EventStatus.PUBLISHED:
        return event
    if event.status in _CLOSED and audit_repository.has_action(
        session, event.id, "event.published"
    ):
        return event
    raise NotFoundError("Event not found")


def list_venues(session: Session) -> list[Venue]:
    return repository.list_venues(session)


def list_public_events(
    session: Session,
    *,
    q: str | None,
    category: str | None,
    city: str | None = None,
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
        city=city,
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


def annotate_on_sale(
    session: Session, events: Iterable[Event], now: datetime | None = None
) -> None:
    """Set what EventOut adds to the columns on each event, each in one batched query for
    all of them: price_from and availability, the venues the calendar_links need (BE-31),
    and the public organizer (BE-29). No query per event.

    Same ticket types as the public ticket type list: visible, inside their sales window.
    """
    events = list(events)
    if not events:
        return
    stats = repository.on_sale_stats(session, [e.id for e in events], now or _now())
    # Loaded into the session, so event.venue below is an identity-map hit, not a query.
    # Kept in a variable: the identity map holds objects weakly.
    venues = repository.load_venues(session, {e.venue_id for e in events})
    organizers = repository.public_organizers(session, [e.organizer_id for e in events])
    for event in events:
        row = stats.get(event.id)
        event.price_from = row.min_price_kzt if row else None
        event.availability = availability_of(row.available, row.quantity_total) if row else None
        event.calendar_links = calendar.links_for(event)
        event.organizer_public = organizers.get(event.organizer_id)
    del venues
