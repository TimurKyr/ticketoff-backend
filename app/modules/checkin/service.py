"""Check-in at the entrance: who may scan, the scan itself, undo, search and stats."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.audit import service as audit
from app.modules.checkin import repository
from app.modules.checkin.schemas import (
    AttendeePage,
    AttendeeRow,
    MyEventOut,
    OfflinePackOut,
    OfflineTicket,
    ScannedTicket,
    ScanOut,
    ScanResult,
    StaffOut,
    StatsOut,
    SyncIn,
    SyncItemIn,
    SyncItemOut,
    SyncOut,
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

# How a ticket was checked in, kept in the audit details ("method").
CHECK_IN_BY_QR = "qr"
CHECK_IN_MANUAL = "manual"
CHECK_IN_OFFLINE = "offline"

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


def _closed_result(event: Event) -> ScanResult | None:
    """The result for every ticket of an event nobody may enter, or None if the door is open."""
    # Nobody gets in to a cancelled event (cancel does not cancel the tickets yet, BE-19).
    # A draft event has no holding orders (unpublish refuses them), so its tickets are invalid.
    if event.status == EventStatus.CANCELLED:
        return ScanResult.EVENT_CANCELLED
    if event.status == EventStatus.DRAFT:
        return ScanResult.INVALID
    return None


def _closed_door(session: Session, event: Event) -> ScanOut | None:
    """The answer for an event nobody may enter, or None if the door is open."""
    result = _closed_result(event)
    return _out(session, event.id, result) if result else None


def _admit_ticket(
    session: Session,
    user: User,
    event: Event,
    ticket: Ticket,
    method: str,
    *,
    checked_in_at: datetime | None = None,
    device_id: str | None = None,
) -> tuple[ScanResult, Ticket]:
    """Check in a ticket the caller has locked (FOR UPDATE), at most once.

    The one check-in path for the QR scan, the manual check-in and the offline sync, so all
    give the same results. Two gates are serialized by the row lock: the second one sees
    `checked_in` and gets `already_used`. The partial unique index on check_in_records is a
    second guard if the lock is ever bypassed. checked_in_at defaults to now (DB time).
    """
    if ticket.status in _RESULT_BY_STATUS:
        return _RESULT_BY_STATUS[ticket.status], ticket

    ticket.status = TicketStatus.CHECKED_IN
    record = CheckInRecord(ticket_id=ticket.id, event_admin_id=user.id)
    if checked_in_at is not None:
        record.checked_in_at = checked_in_at
    session.add(record)
    # Everything that writes stays inside the try: the flush (here or in audit.log) is where
    # uq_check_in_records_active_ticket refuses a gate that lost the race, so it must be
    # caught below; the rollback then drops the audit row too.
    try:
        session.flush()  # inserts the record and gives it its id
        details = {"check_in_record_id": str(record.id), "method": method}
        if device_id is not None:
            details["device_id"] = device_id
        audit.log(
            session,
            actor_user_id=user.id,
            action_type="ticket.checked_in",
            entity_type="ticket",
            entity_id=ticket.id,
            event_id=event.id,
            details=details,
        )
        session.commit()
    except IntegrityError:
        session.rollback()  # another gate won the race
        return ScanResult.ALREADY_USED, session.get(Ticket, ticket.id)
    return ScanResult.CHECKED_IN, ticket


def _admit(session: Session, user: User, event: Event, ticket: Ticket, method: str) -> ScanOut:
    result, ticket = _admit_ticket(session, user, event, ticket, method)
    return _out(session, event.id, result, ticket)


def scan(session: Session, user: User, event_id: uuid.UUID, qr_token: str) -> ScanOut:
    """Validate a QR at the door and admit the ticket holder at most once."""
    event = require_scanner(session, user, event_id)
    if closed := _closed_door(session, event):
        return closed
    token = qr_token.strip()

    # A campaign QR encodes a link ("...?promo=pc1.xxx"); the bare token is the older form.
    if token.startswith(CAMPAIGN_PREFIX) or f"promo={CAMPAIGN_PREFIX}" in token:
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
    return _admit(session, user, event, ticket, CHECK_IN_BY_QR)


def _require_ticket_scanner(session: Session, user: User, ticket_id: uuid.UUID) -> Event:
    """The ticket's event, if the user may scan it; unknown and foreign tickets give one 404.

    Nothing is locked here, so an outsider cannot hold locks on other events' tickets.
    """
    event_id = repository.event_id_of_ticket(session, ticket_id)
    try:
        if event_id is None:
            raise NotFoundError("Ticket not found")
        return require_scanner(session, user, event_id)
    except NotFoundError as exc:
        raise NotFoundError("Ticket not found") from exc


def check_in_ticket(session: Session, user: User, ticket_id: uuid.UUID) -> ScanOut:
    """Admit a guest found by name in the attendee list, without their QR.

    Same rules and results as a scan; the audit row says `method: manual`.
    """
    event = _require_ticket_scanner(session, user, ticket_id)
    if closed := _closed_door(session, event):
        return closed
    ticket = repository.get_ticket_for_update(session, ticket_id)
    if ticket is None:  # deleted in between; tickets are not deleted by the app
        raise NotFoundError("Ticket not found")
    return _admit(session, user, event, ticket, CHECK_IN_MANUAL)


def undo_check_in(session: Session, user: User, ticket_id: uuid.UUID) -> ScannedTicket:
    """Reverse a mistaken check-in: the record stays (is_reversed), the ticket is valid again."""
    event = _require_ticket_scanner(session, user, ticket_id)
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
        event_id=event.id,
        details={"check_in_record_id": str(record.id)},
    )
    session.commit()  # the reversal and its audit row together
    return _scanned(session, ticket)


# --- offline mode (BE-32) -------------------------------------------------------------------


def offline_pack(session: Session, user: User, event_id: uuid.UUID) -> OfflinePackOut:
    """Every ticket of the event with its status, for a scanner that will work offline.

    No QR tokens and no signing secret: offline, a QR is checked by its ticket_id (see
    OFFLINE_QR_HOWTO). The download is audited, since attendee names leave the server.
    """
    event = require_scanner(session, user, event_id)
    generated_at = datetime.now(UTC)
    tickets = [
        OfflineTicket(
            ticket_id=ticket.id,
            status=ticket.status,
            attendee_name=attendee_name,
            ticket_type=ticket_type,
        )
        for ticket, attendee_name, ticket_type in repository.offline_pack_rows(session, event.id)
    ]
    registered, checked_in = repository.ticket_counts(session, event.id)
    pack = OfflinePackOut(
        event_id=event.id,
        event_title=event.title,
        event_status=event.status,
        generated_at=generated_at,
        checked_in_count=checked_in,
        total_count=registered,
        tickets=tickets,
    )
    audit.log(
        session,
        actor_user_id=user.id,
        action_type="event.offline_pack_downloaded",
        entity_type="event",
        entity_id=event.id,
        event_id=event.id,
        details={"tickets": len(tickets)},
    )
    session.commit()
    return pack


def _sync_ticket_id(raw: str) -> tuple[uuid.UUID | None, ScanResult | None]:
    """(ticket id, None) for a usable item, or (None, the result to answer with)."""
    token = raw.strip()
    if token.startswith(CAMPAIGN_PREFIX) or f"promo={CAMPAIGN_PREFIX}" in token:
        return None, ScanResult.CAMPAIGN_QR_REJECTED
    if token.startswith(TICKET_PREFIX):  # the whole QR text: its signature must hold
        ticket_id = verify_ticket_token(token)
        return (ticket_id, None) if ticket_id else (None, ScanResult.INVALID)
    try:
        ticket_id = uuid.UUID(token)
    except ValueError:
        return None, ScanResult.INVALID
    if token.lower() != str(ticket_id):  # only the canonical spelling, as in the QR
        return None, ScanResult.INVALID
    return ticket_id, None


def _sync_item(
    session: Session,
    user: User,
    event: Event,
    item: SyncItemIn,
    now: datetime,
    closed: ScanResult | None,
) -> SyncItemOut:
    out = SyncItemOut(ticket_id=item.ticket_id, device_id=item.device_id, result=ScanResult.INVALID)
    if closed is not None:
        out.result = closed
        return out
    ticket_id, rejected = _sync_ticket_id(item.ticket_id)
    if rejected is not None:
        out.result = rejected
        return out
    ticket_event_id = repository.event_id_of_ticket(session, ticket_id)
    if ticket_event_id is None:
        return out  # invalid
    if ticket_event_id != event.id:
        out.result = ScanResult.WRONG_EVENT
        return out
    ticket = repository.get_ticket_for_update(session, ticket_id)
    if ticket is None:  # deleted in between; tickets are not deleted by the app
        return out
    # The device's time of the scan, but never later than now (a clock running ahead).
    out.result, ticket = _admit_ticket(
        session,
        user,
        event,
        ticket,
        CHECK_IN_OFFLINE,
        checked_in_at=min(item.scanned_at, now),
        device_id=item.device_id,
    )
    out.ticket = _scanned(session, ticket)
    record = repository.active_check_in(session, ticket.id)
    if record is not None:
        out.checked_in_at = record.checked_in_at
        out.checked_in_by, out.checked_in_device_id = repository.check_in_info(session, record)
    session.commit()  # ends the transaction: no row lock outlives its item
    return out


def sync(session: Session, user: User, payload: SyncIn) -> SyncOut:
    """Upload the check-ins a scanner made offline; one result per item, in the sent order.

    Every item goes through the same check-in as /scan, one transaction per item, so a
    ticket scanned on two devices is admitted once: the first sync wins, the other item
    gets already_used with who and when. Items of one batch are applied oldest scan first.
    """
    event = require_scanner(session, user, payload.event_id)
    closed = _closed_result(event)
    now = datetime.now(UTC)
    results: dict[int, SyncItemOut] = {}
    for index in sorted(range(len(payload.items)), key=lambda i: payload.items[i].scanned_at):
        results[index] = _sync_item(session, user, event, payload.items[index], now, closed)
    registered, checked_in = repository.ticket_counts(session, event.id)
    return SyncOut(
        items=[results[i] for i in range(len(payload.items))],
        checked_in_count=checked_in,
        total_count=registered,
    )


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
