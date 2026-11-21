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


def _page(
    session: Session,
    filters: list[ColumnElement[bool]],
    *,
    newest_first: bool,
    limit: int,
    offset: int,
) -> tuple[list[SupportCase], int]:
    total = session.scalar(select(func.count()).select_from(SupportCase).where(*filters)) or 0
    order = (
        (SupportCase.created_at.desc(), SupportCase.id)
        if newest_first
        else (SupportCase.created_at, SupportCase.id)
    )
    rows = session.scalars(
        select(SupportCase).where(*filters).order_by(*order).limit(limit).offset(offset)
    )
    return list(rows), total


def list_opened_by(
    session: Session,
    user_id: uuid.UUID,
    *,
    status: SupportStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[SupportCase], int]:
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
) -> tuple[list[SupportCase], int]:
    """Cases a handler works on, oldest first (the longest-waiting case comes first).

    organizer_id None means a platform admin: every case. Otherwise only attendee cases
    about events of that organizer.
    """
    filters: list[ColumnElement[bool]] = []
    if organizer_id is not None:
        own_events = select(Event.id).where(Event.organizer_id == organizer_id)
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
