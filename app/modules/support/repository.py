import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile, User
from app.modules.ops.enums import SupportKind, SupportStatus
from app.modules.ops.models import SupportCase, SupportMessage
from app.modules.orders.models import Order, OrderItem, Ticket


def add(session: Session, row: SupportCase | SupportMessage) -> None:
    session.add(row)
    session.flush()


def get_case(
    session: Session, case_id: uuid.UUID, *, for_update: bool = False
) -> SupportCase | None:
    """The case, locked until the end of the transaction when it is about to be changed.

    The lock makes two people acting on one case (two agents taking it, a reply arriving
    while it is resolved) happen one after the other, not both on stale data.
    """
    stmt = select(SupportCase).where(SupportCase.id == case_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalar(stmt)


def list_messages(session: Session, case_id: uuid.UUID) -> list[SupportMessage]:
    # id breaks ties between messages written in the same instant, so the order is stable.
    stmt = (
        select(SupportMessage)
        .where(SupportMessage.case_id == case_id)
        .order_by(SupportMessage.created_at, SupportMessage.id)
    )
    return list(session.scalars(stmt))


@dataclass(frozen=True, slots=True)
class CaseRow:
    """A case with what is shown next to it: the opener's email and the event's title."""

    case: SupportCase
    opener_email: str
    event_title: str | None


def _with_display(*columns):
    """SELECT columns + the opener's email + the event's title (NULL without an event), FROM
    support_cases joined to users (opener) and outer-joined to events."""
    return (
        select(*columns, User.email, Event.title)
        .select_from(SupportCase)
        .join(User, User.id == SupportCase.opened_by)
        .outerjoin(Event, Event.id == SupportCase.event_id)
    )


def case_display(session: Session, case_id: uuid.UUID) -> tuple[str, str | None]:
    """(opener email, event title or None) of one case, in one query."""
    email, title = session.execute(_with_display().where(SupportCase.id == case_id)).one()
    return email, title


def _page(
    session: Session,
    filters: list[ColumnElement[bool]],
    *,
    newest_first: bool,
    limit: int,
    offset: int,
) -> tuple[list[CaseRow], int]:
    """One page of cases with their display fields in one query, plus the count."""
    total = session.scalar(select(func.count()).select_from(SupportCase).where(*filters)) or 0
    order = (
        (SupportCase.created_at.desc(), SupportCase.id)
        if newest_first
        else (SupportCase.created_at, SupportCase.id)
    )
    rows = session.execute(
        _with_display(SupportCase).where(*filters).order_by(*order).limit(limit).offset(offset)
    )
    return [CaseRow(case, email, title) for case, email, title in rows], total


def list_opened_by(
    session: Session,
    user_id: uuid.UUID,
    *,
    status: SupportStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[CaseRow], int]:
    """The user's own cases, newest first."""
    filters = [SupportCase.opened_by == user_id]
    if status is not None:
        filters.append(SupportCase.status == status)
    return _page(session, filters, newest_first=True, limit=limit, offset=offset)


def list_inbox(
    session: Session,
    *,
    organizer_id: uuid.UUID | None,
    status: SupportStatus | None,
    kind: SupportKind | None,
    limit: int,
    offset: int,
) -> tuple[list[CaseRow], int]:
    """Cases a handler works on, oldest first (the longest-waiting case comes first).

    organizer_id None means a platform admin: every case. Otherwise only attendee cases
    about events of that organizer.
    """
    filters: list[ColumnElement[bool]] = []
    if organizer_id is not None:
        # correlate(None): the page query also joins events (for the title); without it
        # this subquery would bind to that outer row instead of listing the organizer's events.
        own_events = select(Event.id).where(Event.organizer_id == organizer_id).correlate(None)
        filters += [SupportCase.kind == SupportKind.ATTENDEE, SupportCase.event_id.in_(own_events)]
    if status is not None:
        filters.append(SupportCase.status == status)
    if kind is not None:
        filters.append(SupportCase.kind == kind)
    return _page(session, filters, newest_first=False, limit=limit, offset=offset)


def organizer_id_of(session: Session, user_id: uuid.UUID) -> uuid.UUID | None:
    return session.scalar(select(OrganizerProfile.id).where(OrganizerProfile.user_id == user_id))


def get_event(session: Session, event_id: uuid.UUID) -> Event | None:
    return session.get(Event, event_id)


def get_order(session: Session, order_id: uuid.UUID) -> Order | None:
    return session.get(Order, order_id)


@dataclass(frozen=True, slots=True)
class TicketOwner:
    """Who bought a ticket, and for which event."""

    buyer_user_id: uuid.UUID
    event_id: uuid.UUID


def get_ticket_owner(session: Session, ticket_id: uuid.UUID) -> TicketOwner | None:
    row = session.execute(
        select(Order.buyer_user_id, Order.event_id)
        .select_from(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Ticket.id == ticket_id)
    ).one_or_none()
    return TicketOwner(row[0], row[1]) if row else None


def get_user(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)
