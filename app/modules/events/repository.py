import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import EventBucket


def list_venues(session: Session) -> list[Venue]:
    return list(session.scalars(select(Venue).order_by(Venue.name)))


def get_venue(session: Session, venue_id: uuid.UUID) -> Venue | None:
    return session.get(Venue, venue_id)


def get_event(session: Session, event_id: uuid.UUID) -> Event | None:
    return session.get(Event, event_id)


def add_event(session: Session, event: Event) -> Event:
    session.add(event)
    session.flush()
    return event


def _page(session: Session, stmt, limit: int, offset: int) -> tuple[list[Event], int]:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = list(session.scalars(stmt.limit(limit).offset(offset)))
    return items, total


def list_public(
    session: Session,
    *,
    now: datetime,
    q: str | None,
    category: str | None,
    from_date: datetime | None,
    to_date: datetime | None,
    include_past: bool,
    limit: int,
    offset: int,
) -> tuple[list[Event], int]:
    """Published + public events only, soonest first."""
    stmt = select(Event).where(
        Event.status == EventStatus.PUBLISHED,
        Event.visibility == EventVisibility.PUBLIC,
    )
    if q:
        stmt = stmt.where(Event.title.ilike(f"%{q}%"))
    if category:
        stmt = stmt.where(Event.category == category)
    if from_date:
        stmt = stmt.where(Event.start_at >= from_date)
    if to_date:
        stmt = stmt.where(Event.start_at <= to_date)
    if not include_past and not from_date:
        stmt = stmt.where(func.coalesce(Event.end_at, Event.start_at) >= now)
    return _page(session, stmt.order_by(Event.start_at.asc(), Event.id.asc()), limit, offset)


def list_for_organizer(
    session: Session,
    *,
    organizer_id: uuid.UUID,
    bucket: EventBucket | None,
    now: datetime,
    limit: int,
    offset: int,
) -> tuple[list[Event], int]:
    """Events of one organizer, optionally narrowed to a dashboard bucket.

    Bucket rules (effective end = end_at, or start_at when end_at is empty):
      cancelled -> status cancelled
      completed -> status completed, or effective end is in the past
      active    -> running right now
      upcoming  -> starts in the future
    """
    effective_end = func.coalesce(Event.end_at, Event.start_at)
    not_closed = Event.status.notin_([EventStatus.CANCELLED, EventStatus.COMPLETED])

    stmt = select(Event).where(Event.organizer_id == organizer_id)
    if bucket == EventBucket.CANCELLED:
        stmt = stmt.where(Event.status == EventStatus.CANCELLED)
    elif bucket == EventBucket.COMPLETED:
        stmt = stmt.where(
            Event.status != EventStatus.CANCELLED,
            (Event.status == EventStatus.COMPLETED) | (effective_end < now),
        )
    elif bucket == EventBucket.ACTIVE:
        stmt = stmt.where(not_closed, Event.start_at <= now, effective_end >= now)
    elif bucket == EventBucket.UPCOMING:
        stmt = stmt.where(not_closed, Event.start_at > now)
    return _page(session, stmt.order_by(Event.start_at.desc(), Event.id.asc()), limit, offset)
