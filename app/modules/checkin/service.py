"""Check-in at the entrance: who may scan, the scan itself, undo, search and stats."""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.audit import service as audit
from app.modules.checkin import repository
from app.modules.checkin.schemas import (
    AttendeePage,
    AttendeeRow,
    MyEventOut,
    ScannedTicket,
    ScanOut,
    ScanResult,
    StaffOut,
    StatsOut,
)
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import StaffAssignment, User
from app.modules.ops.models import CheckInRecord
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Ticket
from app.modules.orders.qr import TICKET_PREFIX, verify_ticket_token

# QR prefixes decide whose token it is (see the BE-13 / BE-16 task descriptions).
CAMPAIGN_PREFIX = "pc1."

_RESULT_BY_STATUS = {
    TicketStatus.CHECKED_IN: ScanResult.ALREADY_USED,
    TicketStatus.CANCELLED: ScanResult.CANCELLED,
    TicketStatus.REFUNDED: ScanResult.REFUNDED,
}


# --- access ---------------------------------------------------------------------------------


def _is_owner(session: Session, user: User, event: Event) -> bool:
    return event.organizer_id == repository.owner_profile_id(session, user.id)


def require_scanner(session: Session, user: User, event_id: uuid.UUID) -> Event:
    """The event, if the user owns it or is assigned as staff to it.

    Someone else's event answers 404 like a missing one, so a draft or private event is
    never revealed by its UUID.
    """
    event = repository.get_event(session, event_id)
    if event is None or (
        not _is_owner(session, user, event) and not repository.get_staff(session, event.id, user.id)
    ):
        raise NotFoundError("Event not found")
    return event


def _require_owner(session: Session, user: User, event_id: uuid.UUID) -> Event:
    event = repository.get_event(session, event_id)
    if event is None or not _is_owner(session, user, event):
        raise NotFoundError("Event not found")  # do not reveal other organizers' events
    return event


# --- staff management (event owner) ---------------------------------------------------------


def add_staff(session: Session, owner: User, event_id: uuid.UUID, email: str) -> StaffOut:
    event = _require_owner(session, owner, event_id)
    user = repository.get_user_by_email(session, email)
    if user is None:
        raise NotFoundError("No user with this email; ask them to register first")
    if repository.get_staff(session, event.id, user.id):
        raise BusinessRuleError("This user is already staff for the event")
    assignment = StaffAssignment(event_id=event.id, user_id=user.id)
    session.add(assignment)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BusinessRuleError("This user is already staff for the event") from exc
    return StaffOut(user_id=user.id, email=user.email, role=assignment.role)


def list_staff(session: Session, owner: User, event_id: uuid.UUID) -> list[StaffOut]:
    event = _require_owner(session, owner, event_id)
    return [
        StaffOut(user_id=user.id, email=user.email, role=assignment.role)
        for assignment, user in repository.list_staff(session, event.id)
    ]


def remove_staff(session: Session, owner: User, event_id: uuid.UUID, user_id: uuid.UUID) -> None:
    event = _require_owner(session, owner, event_id)
    assignment = repository.get_staff(session, event.id, user_id)
    if assignment is None:
        raise NotFoundError("This user is not staff for the event")
    session.delete(assignment)
    session.commit()


def my_events(session: Session, user: User) -> list[MyEventOut]:
    return [
        MyEventOut(
            id=event.id,
            title=event.title,
            status=event.status,
            start_at=event.start_at,
            end_at=event.end_at,
            venue_name=event.venue.name,
            venue_address=event.venue.address,
            timezone=event.venue.timezone,
            is_owner=is_owner,
        )
        for event, is_owner in repository.list_scannable_events(session, user.id)
    ]


# --- scanning -------------------------------------------------------------------------------


def _scanned(session: Session, ticket: Ticket) -> ScannedTicket:
    attendee_name, ticket_type = repository.ticket_details(session, ticket)
    return ScannedTicket(
        id=ticket.id,
        attendee_name=attendee_name,
        ticket_type=ticket_type,
        seat_id=ticket.seat_id,
        status=ticket.status,
    )


def _out(
    session: Session, event_id: uuid.UUID, result: ScanResult, ticket: Ticket | None = None
) -> ScanOut:
    registered, checked_in = repository.ticket_counts(session, event_id)
    return ScanOut(
        result=result,
        ticket=_scanned(session, ticket) if ticket else None,
        checked_in_count=checked_in,
        total_count=registered,
    )


def scan(session: Session, user: User, event_id: uuid.UUID, qr_token: str) -> ScanOut:
    """Validate a QR at the door and admit the ticket holder at most once.

    The ticket row is locked (FOR UPDATE), so two gates scanning the same QR are serialized:
    the second one sees `checked_in` and gets `already_used`. The partial unique index on
    check_in_records is a second guard if the lock is ever bypassed.
    """
    event = require_scanner(session, user, event_id)
    # Nobody gets in to a cancelled event (cancel does not cancel the tickets yet, BE-19).
    # A draft event has no holding orders (unpublish refuses them), so its QRs are invalid.
    if event.status == EventStatus.CANCELLED:
        return _out(session, event.id, ScanResult.EVENT_CANCELLED)
    if event.status == EventStatus.DRAFT:
        return _out(session, event.id, ScanResult.INVALID)
    token = qr_token.strip()

    if token.startswith(CAMPAIGN_PREFIX):
        return _out(session, event.id, ScanResult.CAMPAIGN_QR_REJECTED)
    if not token.startswith(TICKET_PREFIX):
        return _out(session, event.id, ScanResult.INVALID)
    # Forged or damaged tokens are rejected by the HMAC check, without a DB lookup.
    if verify_ticket_token(token) is None:
        return _out(session, event.id, ScanResult.INVALID)
    ticket = repository.find_ticket_for_update(session, token)
    if ticket is None:
        return _out(session, event.id, ScanResult.INVALID)
    if repository.ticket_event_id(session, ticket) != event.id:
        return _out(session, event.id, ScanResult.WRONG_EVENT)
    if ticket.status in _RESULT_BY_STATUS:
        return _out(session, event.id, _RESULT_BY_STATUS[ticket.status], ticket)

    ticket.status = TicketStatus.CHECKED_IN
    record = CheckInRecord(ticket_id=ticket.id, event_admin_id=user.id)
    session.add(record)
    # Everything that writes stays inside the try: the flush (here or in audit.log) is where
    # uq_check_in_records_active_ticket refuses a gate that lost the race, so it must be
    # caught below; the rollback then drops the audit row too.
    try:
        session.flush()  # inserts the record and gives it its id
        audit.log(
            session,
            actor_user_id=user.id,
            action_type="ticket.checked_in",
            entity_type="ticket",
            entity_id=ticket.id,
            event_id=event.id,
            details={"check_in_record_id": str(record.id)},
        )
        session.commit()
    except IntegrityError:
        session.rollback()  # another gate won the race
        return _out(session, event.id, ScanResult.ALREADY_USED, session.get(Ticket, ticket.id))
    return _out(session, event.id, ScanResult.CHECKED_IN, ticket)


def undo_check_in(session: Session, user: User, ticket_id: uuid.UUID) -> ScannedTicket:
    """Reverse a mistaken scan: the record stays (is_reversed) and the ticket is valid again.

    Access is checked before the ticket row is locked, so an outsider cannot hold locks on
    other events' tickets. Unknown and foreign tickets give the same 404.
    """
    event_id = repository.event_id_of_ticket(session, ticket_id)
    try:
        if event_id is None:
            raise NotFoundError("Ticket not found")
        require_scanner(session, user, event_id)
    except NotFoundError as exc:
        raise NotFoundError("Ticket not found") from exc
    ticket = repository.get_ticket_for_update(session, ticket_id)
    if ticket is None:  # deleted in between; tickets are not deleted by the app
        raise NotFoundError("Ticket not found")
    record = repository.active_check_in(session, ticket.id)
    if ticket.status != TicketStatus.CHECKED_IN or record is None:
        raise BusinessRuleError("This ticket is not checked in")

    record.is_reversed = True
    ticket.status = TicketStatus.VALID
    audit.log(
        session,
        actor_user_id=user.id,
        action_type="ticket.check_in_reversed",
        entity_type="ticket",
        entity_id=ticket.id,
        event_id=event_id,
        details={"check_in_record_id": str(record.id)},
    )
    session.commit()  # the reversal and its audit row together
    return _scanned(session, ticket)


# --- door-side lists ------------------------------------------------------------------------


def search_attendees(
    session: Session, user: User, event_id: uuid.UUID, q: str | None, limit: int, offset: int
) -> AttendeePage:
    event = require_scanner(session, user, event_id)
    rows, total = repository.search_attendees(session, event.id, q, limit, offset)
    items = [
        AttendeeRow(
            ticket_id=ticket.id,
            attendee_name=attendee.name,
            attendee_email=attendee.email,
            ticket_type=ticket_type,
            status=ticket.status,
        )
        for ticket, attendee, ticket_type in rows
    ]
    return AttendeePage(items=items, total=total, limit=limit, offset=offset)


def stats(session: Session, user: User, event_id: uuid.UUID) -> StatsOut:
    event = require_scanner(session, user, event_id)
    registered, checked_in = repository.ticket_counts(session, event.id)
    percent = round(100 * checked_in / registered, 1) if registered else 0.0
    return StatsOut(
        registered=registered,
        checked_in=checked_in,
        absent=registered - checked_in,
        percent=percent,
    )
