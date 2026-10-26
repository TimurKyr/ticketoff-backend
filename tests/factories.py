"""Minimal row builders for DB tests. They flush, never commit."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.orders.enums import OrderStatus, TicketKind
from app.modules.orders.models import Order, OrderItem, TicketType


def create_user(session: Session) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    session.add(user)
    session.flush()
    return user


def create_organizer(session: Session) -> OrganizerProfile:
    profile = OrganizerProfile(user_id=create_user(session).id, display_name="Organizer")
    session.add(profile)
    session.flush()
    return profile


def create_event(
    session: Session,
    organizer: OrganizerProfile,
    status: EventStatus = EventStatus.PUBLISHED,
) -> Event:
    venue = Venue(name="Hall")
    session.add(venue)
    session.flush()
    event = Event(
        organizer_id=organizer.id,
        venue_id=venue.id,
        title="Concert",
        status=status,
        start_at=datetime.now(UTC) + timedelta(days=30),
    )
    session.add(event)
    session.flush()
    return event


def create_ticket_type(session: Session, event: Event, **overrides: object) -> TicketType:
    fields: dict[str, object] = {
        "name": "General",
        "kind": TicketKind.PAID,
        "price_kzt": 5000,
        "quantity_total": 100,
        "max_per_order": 10,
    }
    ticket_type = TicketType(event_id=event.id, **(fields | overrides))
    session.add(ticket_type)
    session.flush()
    return ticket_type


def create_order_item(
    session: Session,
    ticket_type: TicketType,
    quantity: int,
    status: OrderStatus = OrderStatus.PAID,
) -> OrderItem:
    order = Order(
        buyer_user_id=create_user(session).id,
        event_id=ticket_type.event_id,
        status=status,
    )
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id,
        ticket_type_id=ticket_type.id,
        quantity=quantity,
        unit_price_kzt=ticket_type.price_kzt,
    )
    session.add(item)
    session.flush()
    return item
