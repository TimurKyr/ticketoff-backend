import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import EventBucket
from app.modules.identity.models import OrganizerProfile
from app.modules.orders.models import Order, OrderItem, TicketType
from app.modules.orders.repository import (
    checkout_hold,
    holding_orders_clause,
    sales_window_clause,
)


def list_venues(session: Session) -> list[Venue]:
    return list(session.scalars(select(Venue).order_by(Venue.name)))


def load_venues(session: Session, venue_ids: Iterable[uuid.UUID]) -> list[Venue]:
    ids = list(venue_ids)
    if not ids:
        return []
    return list(session.scalars(select(Venue).where(Venue.id.in_(ids))))


def get_venue(session: Session, venue_id: uuid.UUID) -> Venue | None:
    return session.get(Venue, venue_id)


def get_event(session: Session, event_id: uuid.UUID) -> Event | None:
    return session.get(Event, event_id)


def has_orders(session: Session, event_id: uuid.UUID, now: datetime) -> bool:
    """Whether any order holds tickets for the event at `now` (expired checkouts don't)."""
    stmt = (
        select(Order.id)
        .where(Order.event_id == event_id, holding_orders_clause(now, checkout_hold(session)))
        .limit(1)
    )
    return session.scalar(stmt) is not None


def _escape_like(value: str) -> str:
    """Make % and _ in user input match literally (escape character is /)."""
    return value.replace("/", "//").replace("%", "/%").replace("_", "/_")


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
    city: str | None,
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
        stmt = stmt.where(Event.title.ilike(f"%{_escape_like(q)}%", escape="/"))
    if category:
        stmt = stmt.where(Event.category == category)
    if city:
        stmt = stmt.join(Venue, Venue.id == Event.venue_id).where(Venue.city == city)
    if from_date:
        stmt = stmt.where(Event.start_at >= from_date)
    if to_date:
        stmt = stmt.where(Event.start_at <= to_date)
    # from_date narrows the list further; it never brings back events that already ended.
    if not include_past:
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


@dataclass(frozen=True)
class PublicOrganizer:
    """The organizer fields EventOut shows publicly (no phone, payout or verification)."""

    id: uuid.UUID
    display_name: str
    contact_email: str | None


def public_organizers(
    session: Session, organizer_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, PublicOrganizer]:
    """The public fields of these organizers, in one query (only the needed columns)."""
    ids = set(organizer_ids)
    if not ids:
        return {}
    stmt = select(
        OrganizerProfile.id, OrganizerProfile.display_name, OrganizerProfile.contact_email
    ).where(OrganizerProfile.id.in_(ids))
    return {row[0]: PublicOrganizer(*row) for row in session.execute(stmt)}


@dataclass(frozen=True)
class OnSaleStats:
    """Visible ticket types of one event that are on sale (the public ticket type list)."""

    min_price_kzt: int
    available: int  # summed over those types, never below 0 per type
    quantity_total: int


def on_sale_stats(
    session: Session, event_ids: Iterable[uuid.UUID], now: datetime
) -> dict[uuid.UUID, OnSaleStats]:
    """Per event: the same ticket types as GET /events/{id}/ticket-types/public (visible,
    sales window contains `now`, sold-out ones included), aggregated in one query for all
    the events. Events with no such type are absent from the result.

    Held quantities use holding_orders_clause, as get_availability does.
    """
    ids = list(event_ids)
    if not ids:
        return {}
    held = (
        select(OrderItem.ticket_type_id, func.sum(OrderItem.quantity).label("held"))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id.in_(ids), holding_orders_clause(now, checkout_hold(session)))
        .group_by(OrderItem.ticket_type_id)
        .subquery()
    )
    left = TicketType.quantity_total - func.coalesce(held.c.held, 0)
    stmt = (
        select(
            TicketType.event_id,
            func.min(TicketType.price_kzt),
            func.sum(case((left > 0, left), else_=0)),
            func.sum(TicketType.quantity_total),
        )
        .outerjoin(held, held.c.ticket_type_id == TicketType.id)
        .where(
            TicketType.event_id.in_(ids),
            TicketType.is_hidden.is_(False),
            sales_window_clause(now),
        )
        .group_by(TicketType.event_id)
    )
    return {
        event_id: OnSaleStats(int(price), int(available), int(total))
        for event_id, price, available, total in session.execute(stmt)
    }
