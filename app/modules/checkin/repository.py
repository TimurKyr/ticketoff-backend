import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile, StaffAssignment, User
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType

# Tickets that count as "registered" for the door: issued and not cancelled or refunded.
ADMISSIBLE_STATUSES = (TicketStatus.VALID, TicketStatus.CHECKED_IN)


def get_event(session: Session, event_id: uuid.UUID) -> Event | None:
    return session.get(Event, event_id)


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == email.strip().lower()))


def owner_profile_id(session: Session, user_id: uuid.UUID) -> uuid.UUID | None:
    return session.scalar(select(OrganizerProfile.id).where(OrganizerProfile.user_id == user_id))


def get_staff(session: Session, event_id: uuid.UUID, user_id: uuid.UUID) -> StaffAssignment | None:
    return session.scalar(
        select(StaffAssignment).where(
            StaffAssignment.event_id == event_id, StaffAssignment.user_id == user_id
        )
    )


def list_staff(session: Session, event_id: uuid.UUID) -> list[tuple[StaffAssignment, User]]:
    stmt = (
        select(StaffAssignment, User)
        .join(User, User.id == StaffAssignment.user_id)
        .where(StaffAssignment.event_id == event_id)
        .order_by(User.email)
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]


def list_scannable_events(session: Session, user_id: uuid.UUID) -> list[tuple[Event, bool]]:
    """Events the user staffs or owns (not cancelled), soonest first, with an is_owner flag."""
    profile_id = owner_profile_id(session, user_id)
    staffed = select(StaffAssignment.event_id).where(StaffAssignment.user_id == user_id)
    stmt = select(Event).where(
        Event.status != EventStatus.CANCELLED,
        or_(Event.id.in_(staffed), Event.organizer_id == profile_id),
    )
    events = session.scalars(stmt.order_by(Event.start_at.asc(), Event.id.asc())).all()
    return [(event, event.organizer_id == profile_id) for event in events]


def find_ticket_for_update(session: Session, qr_token: str) -> Ticket | None:
    """Lock the ticket row so two scans of the same QR are handled one after another."""
    return session.scalar(select(Ticket).where(Ticket.qr_token == qr_token).with_for_update())


def get_ticket_for_update(session: Session, ticket_id: uuid.UUID) -> Ticket | None:
    return session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())


def ticket_event_id(session: Session, ticket: Ticket) -> uuid.UUID:
    return session.scalar(
        select(Order.event_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == ticket.order_item_id)
    )


def ticket_details(session: Session, ticket: Ticket) -> tuple[str, str]:
    """(attendee name, ticket type name)."""
    row = session.execute(
        select(Attendee.name, TicketType.name)
        .select_from(Ticket)
        .join(Attendee, Attendee.id == Ticket.attendee_id)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(TicketType, TicketType.id == OrderItem.ticket_type_id)
        .where(Ticket.id == ticket.id)
    ).one()
    return row[0], row[1]


def active_check_in(session: Session, ticket_id: uuid.UUID) -> CheckInRecord | None:
    return session.scalar(
        select(CheckInRecord).where(
            CheckInRecord.ticket_id == ticket_id, CheckInRecord.is_reversed.is_(False)
        )
    )


def _event_tickets(event_id: uuid.UUID):
    return (
        select(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.event_id == event_id)
    )


def ticket_counts(session: Session, event_id: uuid.UUID) -> tuple[int, int]:
    """(registered, checked_in) for the event."""
    tickets = _event_tickets(event_id).where(Ticket.status.in_(ADMISSIBLE_STATUSES)).subquery()
    stmt = select(
        func.count(),
        func.count().filter(tickets.c.status == TicketStatus.CHECKED_IN),
    ).select_from(tickets)
    registered, checked_in = session.execute(stmt).one()
    return registered, checked_in


def _escape_like(value: str) -> str:
    return value.replace("/", "//").replace("%", "/%").replace("_", "/_")


def search_attendees(
    session: Session, event_id: uuid.UUID, q: str | None, limit: int
) -> list[tuple[Ticket, Attendee, str]]:
    stmt = (
        _event_tickets(event_id)
        .join(Attendee, Attendee.id == Ticket.attendee_id)
        .join(TicketType, TicketType.id == OrderItem.ticket_type_id)
        .add_columns(Attendee, TicketType.name)
    )
    if q:
        pattern = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(Attendee.name.ilike(pattern, escape="/"), Attendee.email.ilike(pattern, escape="/"))
        )
    stmt = stmt.order_by(Attendee.name.asc(), Ticket.id.asc()).limit(limit)
    return [(row[0], row[1], row[2]) for row in session.execute(stmt)]
