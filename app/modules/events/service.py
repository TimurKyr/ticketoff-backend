import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.events import repository
from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event
from app.modules.events.schemas import EventCreate, EventUpdate, check_dates
from app.modules.identity.models import OrganizerProfile

_CLOSED = (EventStatus.CANCELLED, EventStatus.COMPLETED)


class EventServiceError(Exception):
    """Business-rule violation. The router turns it into an HTTP error."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """SQLite (used only in local checks) drops tzinfo; Postgres keeps it."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _require_venue(session: Session, venue_id: uuid.UUID) -> None:
    if repository.get_venue(session, venue_id) is None:
        raise EventServiceError(422, "Venue does not exist")


def _require_open(event: Event) -> None:
    if event.status in _CLOSED:
        raise EventServiceError(409, f"Event is {event.status.value} and can no longer be changed")


def create_event(session: Session, organizer: OrganizerProfile, payload: EventCreate) -> Event:
    _require_venue(session, payload.venue_id)
    event = Event(organizer_id=organizer.id, status=EventStatus.DRAFT, **payload.model_dump())
    repository.add_event(session, event)
    session.commit()
    return event


def update_event(session: Session, event: Event, payload: EventUpdate) -> Event:
    _require_open(event)
    changes = payload.model_dump(exclude_unset=True)
    if "venue_id" in changes and changes["venue_id"] is not None:
        _require_venue(session, changes["venue_id"])

    # Re-check date rules against the merged result, not only the sent fields.
    merged = {
        name: changes.get(name, getattr(event, name))
        for name in ("start_at", "end_at", "registration_opens_at", "registration_closes_at")
    }
    try:
        check_dates(**{k: (_as_aware(v) if v else v) for k, v in merged.items()})
    except ValueError as exc:
        raise EventServiceError(422, str(exc)) from exc

    for name, value in changes.items():
        if value is None and name in {
            "title",
            "venue_id",
            "start_at",
            "visibility",
            "seating_type",
        }:
            continue  # required columns cannot be cleared
        setattr(event, name, value)
    session.commit()
    return event


def duplicate_event(session: Session, event: Event) -> Event:
    """Copy the configuration only: no orders, tickets, payments or check-ins."""
    copy = Event(
        organizer_id=event.organizer_id,
        venue_id=event.venue_id,
        title=event.title,
        description=event.description,
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
    session.commit()
    return copy


def publish_event(session: Session, event: Event) -> Event:
    if event.status != EventStatus.DRAFT:
        raise EventServiceError(409, "Only a draft event can be published")
    effective_end = _as_aware(event.end_at or event.start_at)
    if effective_end < _now():
        raise EventServiceError(409, "An event that has already ended cannot be published")
    # TODO(BE-12): also require at least one visible ticket type.
    event.status = EventStatus.PUBLISHED
    session.commit()  # TODO(BE-19): write an audit-log entry.
    return event


def unpublish_event(session: Session, event: Event) -> Event:
    if event.status != EventStatus.PUBLISHED:
        raise EventServiceError(409, "Only a published event can be unpublished")
    event.status = EventStatus.DRAFT
    session.commit()  # TODO(BE-19): write an audit-log entry.
    return event


def cancel_event(session: Session, event: Event) -> Event:
    if event.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
        raise EventServiceError(409, f"A {event.status.value} event cannot be cancelled")
    event.status = EventStatus.CANCELLED
    session.commit()  # TODO(BE-19): audit log; BE-18: notify ticket holders.
    return event


def get_public_event(session: Session, event_id: uuid.UUID) -> Event:
    """Published public/unlisted events only. Everything else is a plain 404,
    so the API never reveals that a private or draft event exists."""
    event = repository.get_event(session, event_id)
    if (
        event is None
        or event.status != EventStatus.PUBLISHED
        or event.visibility == EventVisibility.PRIVATE
    ):
        raise EventServiceError(404, "Event not found")
    return event
