"""Unpublish vs free registration on separate real connections (FX-4, BE-11).

Both lock the event row first, so whichever gets it first wins and the other sees the
committed result: never an order on a draft event. Each test pauses the winner while it
holds the lock and checks that the other side waits.

Rows are committed (the threads cannot see a rolled-back test session) and deleted after.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.db import SessionLocal
from app.modules.events import repository as events_repository
from app.modules.events import service as events_service
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.ops.models import Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import TicketKind
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.schemas import RegistrationCreate
from tests.markers import postgres_only

pytestmark = [postgres_only, pytest.mark.slow]

# Long enough for the waiting side to reach the lock, short enough to keep the suite fast.
WAIT_SECONDS = 0.5


@dataclass
class FreeEvent:
    event_id: uuid.UUID
    ticket_type_id: uuid.UUID
    buyer_id: uuid.UUID


@pytest.fixture
def free_event() -> Iterator[FreeEvent]:
    """A committed published event with one free ticket type and no orders."""
    with SessionLocal() as session:
        organizer_user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        buyer = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}")
        session.add_all([organizer_user, buyer, venue])
        session.flush()
        organizer = OrganizerProfile(user_id=organizer_user.id, display_name="Organizer")
        session.add(organizer)
        session.flush()
        event = Event(
            organizer_id=organizer.id,
            venue_id=venue.id,
            title="Race",
            status=EventStatus.PUBLISHED,
            start_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(event)
        session.flush()
        ticket_type = TicketType(
            event_id=event.id, name="Free", kind=TicketKind.FREE, quantity_total=10
        )
        session.add(ticket_type)
        session.commit()
        data = FreeEvent(event.id, ticket_type.id, buyer.id)
        ids = (venue.id, organizer.id, organizer_user.id)

    yield data

    venue_id, organizer_id, organizer_user_id = ids
    with SessionLocal() as cleanup:
        order_ids = select(Order.id).where(Order.event_id == data.event_id)
        item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        cleanup.execute(delete(Notification).where(Notification.user_id == data.buyer_id))
        cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
        cleanup.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        cleanup.execute(delete(Order).where(Order.event_id == data.event_id))
        cleanup.execute(delete(TicketType).where(TicketType.event_id == data.event_id))
        cleanup.execute(delete(Event).where(Event.id == data.event_id))
        cleanup.execute(delete(Venue).where(Venue.id == venue_id))
        cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == organizer_id))
        cleanup.execute(delete(User).where(User.id.in_([data.buyer_id, organizer_user_id])))
        cleanup.commit()


def _pause_after(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str
) -> tuple[threading.Event, threading.Event]:
    """Make module.name stop after it runs until `release` is set; `reached` signals it."""
    original: Callable = getattr(module, name)
    reached, release = threading.Event(), threading.Event()

    def paused(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        reached.set()
        assert release.wait(timeout=10)
        return result

    monkeypatch.setattr(module, name, paused)
    return reached, release


def _register(data: FreeEvent, results: list[object]) -> None:
    with SessionLocal() as session:
        payload = RegistrationCreate.model_validate(
            {
                "items": [{"ticket_type_id": str(data.ticket_type_id), "quantity": 1}],
                "attendees": [{"name": "Guest"}],
            }
        )
        try:
            buyer = session.get_one(User, data.buyer_id)
            results.append(orders_service.register_free(session, buyer, data.event_id, payload))
        except Exception as exc:  # noqa: BLE001 - checked by the caller
            results.append(exc)


def _unpublish(data: FreeEvent, results: list[object]) -> None:
    with SessionLocal() as session:
        try:
            event = session.get_one(Event, data.event_id)
            results.append(events_service.unpublish_event(session, event))
        except Exception as exc:  # noqa: BLE001 - checked by the caller
            results.append(exc)


def _final_state(data: FreeEvent) -> tuple[EventStatus, int]:
    with SessionLocal() as check:
        status = check.get_one(Event, data.event_id).status
        orders = check.scalar(select(func.count(Order.id)).where(Order.event_id == data.event_id))
        return status, int(orders or 0)


def _race(first: Callable[[], None], second: Callable[[], None], reached, release) -> None:
    first_thread = threading.Thread(target=first)
    first_thread.start()
    assert reached.wait(timeout=10), "the first transaction never took the event lock"

    second_thread = threading.Thread(target=second)
    second_thread.start()
    second_thread.join(timeout=WAIT_SECONDS)
    still_waiting = second_thread.is_alive()

    release.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)
    assert still_waiting, "the second transaction did not wait for the event lock"


def test_registration_holding_the_lock_makes_unpublish_refuse(
    free_event: FreeEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Registration pauses after it locked the event and its ticket types.
    reached, release = _pause_after(monkeypatch, orders_repository, "lock_ticket_types")
    registered: list[object] = []
    unpublished: list[object] = []

    _race(
        lambda: _register(free_event, registered),
        lambda: _unpublish(free_event, unpublished),
        reached,
        release,
    )

    assert not isinstance(registered[0], Exception), registered
    assert isinstance(unpublished[0], BusinessRuleError), unpublished
    assert _final_state(free_event) == (EventStatus.PUBLISHED, 1)


def test_unpublish_holding_the_lock_makes_registration_fail(
    free_event: FreeEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unpublish pauses after it locked the event and found no orders.
    reached, release = _pause_after(monkeypatch, events_repository, "has_orders")
    registered: list[object] = []
    unpublished: list[object] = []

    _race(
        lambda: _unpublish(free_event, unpublished),
        lambda: _register(free_event, registered),
        reached,
        release,
    )

    assert not isinstance(unpublished[0], Exception), unpublished
    assert isinstance(registered[0], NotFoundError), registered
    assert _final_state(free_event) == (EventStatus.DRAFT, 0)
