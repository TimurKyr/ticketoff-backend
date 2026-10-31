import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.orders import repository
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from tests.factories import create_event, create_organizer, create_ticket_type, create_user


def build_order(session: Session, buyer: User, ticket_type: TicketType, quantity: int) -> Order:
    order = Order(
        buyer_user_id=buyer.id,
        event_id=ticket_type.event_id,
        status=OrderStatus.FREE_CONFIRMED,
    )
    item = OrderItem(ticket_type_id=ticket_type.id, quantity=quantity, unit_price_kzt=0)
    order.items.append(item)
    for n in range(quantity):
        attendee = Attendee(name=f"Guest {n}", order=order)
        item.tickets.append(Ticket(attendee=attendee, qr_token=uuid.uuid4().hex))
    repository.add_order(session, order)
    return order


# --- lock_ticket_types ---


def test_lock_returns_requested_types_sorted_by_id(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    types = [create_ticket_type(db_session, event, name=f"T{n}") for n in range(3)]
    other = create_ticket_type(db_session, event, name="Not requested")

    locked = repository.lock_ticket_types(db_session, [t.id for t in reversed(types)])

    assert [t.id for t in locked] == sorted(t.id for t in types)
    assert other not in locked


def test_lock_skips_unknown_ids_and_handles_empty(db_session: Session) -> None:
    assert repository.lock_ticket_types(db_session, []) == []
    assert repository.lock_ticket_types(db_session, [uuid.uuid4()]) == []


def test_lock_blocks_other_transactions() -> None:
    """While one session holds the lock, another cannot lock the same row."""
    with SessionLocal() as setup:
        organizer = create_organizer(setup)
        event = create_event(setup, organizer)
        ticket_type = create_ticket_type(setup, event)
        ids = (ticket_type.id, event.id, event.venue_id, organizer.id, organizer.user_id)
        setup.commit()

    try:
        with SessionLocal() as holder, SessionLocal() as other:
            assert len(repository.lock_ticket_types(holder, [ticket_type.id])) == 1
            with pytest.raises(OperationalError, match="could not obtain lock"):
                other.execute(
                    select(TicketType.id)
                    .where(TicketType.id == ticket_type.id)
                    .with_for_update(nowait=True)
                )
            other.rollback()
            holder.rollback()
            # Released: now the other session gets it.
            assert len(repository.lock_ticket_types(other, [ticket_type.id])) == 1
    finally:
        with SessionLocal() as cleanup:
            for model, row_id in zip(
                (TicketType, Event, Venue, OrganizerProfile, User), ids, strict=True
            ):
                cleanup.execute(delete(model).where(model.id == row_id))
            cleanup.commit()


# --- orders ---


def test_add_order_inserts_items_attendees_and_tickets(db_session: Session) -> None:
    ticket_type = create_ticket_type(
        db_session, create_event(db_session, create_organizer(db_session))
    )
    order = build_order(db_session, create_user(db_session), ticket_type, quantity=2)
    db_session.expire_all()

    loaded = repository.get_order_with_details(db_session, order.id)

    assert loaded is not None
    assert len(loaded.attendees) == 2
    [item] = loaded.items
    assert {t.attendee.name for t in item.tickets} == {"Guest 0", "Guest 1"}


def test_get_order_with_details_unknown_returns_none(db_session: Session) -> None:
    assert repository.get_order_with_details(db_session, uuid.uuid4()) is None


def test_list_orders_for_buyer_returns_only_theirs(db_session: Session) -> None:
    ticket_type = create_ticket_type(
        db_session, create_event(db_session, create_organizer(db_session))
    )
    buyer, someone_else = create_user(db_session), create_user(db_session)
    mine = [build_order(db_session, buyer, ticket_type, 1) for _ in range(2)]
    build_order(db_session, someone_else, ticket_type, 1)

    orders = repository.list_orders_for_buyer(db_session, buyer.id)

    assert [o.id for o in orders] == sorted(o.id for o in mine)


def test_get_ticket_loads_order(db_session: Session) -> None:
    ticket_type = create_ticket_type(
        db_session, create_event(db_session, create_organizer(db_session))
    )
    buyer = create_user(db_session)
    order = build_order(db_session, buyer, ticket_type, 1)
    ticket_id = order.items[0].tickets[0].id
    db_session.expire_all()

    ticket = repository.get_ticket(db_session, ticket_id)

    assert ticket is not None
    assert ticket.order_item.order.buyer_user_id == buyer.id
    assert repository.get_ticket(db_session, uuid.uuid4()) is None
