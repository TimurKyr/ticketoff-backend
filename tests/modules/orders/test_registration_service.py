import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.db import SessionLocal, engine
from app.modules.events.enums import EventStatus, EventVisibility, SeatingType
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.orders import repository, service
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket, TicketType
from app.modules.orders.qr import verify_ticket_token
from app.modules.orders.schemas import RegistrationCreate
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_order_item,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

# Every test registers: FOR UPDATE locks and timezone-aware datetime comparisons.
pytestmark = postgres_only

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
def event(db_session: Session) -> Event:
    return create_event(db_session, create_organizer(db_session))


@pytest.fixture
def buyer(db_session: Session) -> User:
    return create_user(db_session)


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event, quantity_total=10)


def registration(*items: tuple[TicketType, int], attendees: list[dict] | None = None):
    """Request data; by default one generated attendee per ticket."""
    if attendees is None:
        total = sum(q for _, q in items)
        attendees = [{"name": f"Guest {n}", "email": None} for n in range(total)]
    return RegistrationCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(tt.id), "quantity": q} for tt, q in items],
            "attendees": attendees,
        }
    )


def register(session: Session, buyer: User, event: Event, data: RegistrationCreate):
    return service.register_free(session, buyer, event.id, data, now=NOW)


# --- happy paths ---


def test_creates_confirmed_free_order_with_signed_tickets(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    out = register(db_session, buyer, event, registration((free_type, 2)))

    assert out.event_id == event.id
    assert out.status == OrderStatus.FREE_CONFIRMED
    assert out.total_kzt == 0
    assert [(i.ticket_type_id, i.quantity, i.unit_price_kzt) for i in out.items] == [
        (free_type.id, 2, 0)
    ]
    assert len(out.tickets) == 2
    assert {t.status for t in out.tickets} == {TicketStatus.VALID}
    assert len({t.qr_token for t in out.tickets}) == 2
    for ticket in out.tickets:
        assert verify_ticket_token(ticket.qr_token) == ticket.id


def test_rows_are_persisted(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    out = register(db_session, buyer, event, registration((free_type, 2)))
    db_session.expire_all()

    order = db_session.get(Order, out.id)
    assert order is not None
    assert order.buyer_user_id == buyer.id
    assert len(order.attendees) == 2
    stored = db_session.scalars(
        select(Ticket.qr_token).where(Ticket.id.in_([t.id for t in out.tickets]))
    )
    assert set(stored) == {t.qr_token for t in out.tickets}


def test_multiple_ticket_types_and_attendees_in_item_order(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    other_type = create_free_ticket_type(db_session, event, name="Student")
    attendees = [
        {"name": "Aruzhan", "email": "a@example.com"},
        {"name": "Dias", "email": None},
        {"name": "Saule", "email": "s@example.com"},
    ]

    out = register(
        db_session,
        buyer,
        event,
        registration((free_type, 1), (other_type, 2), attendees=attendees),
    )

    assert {(i.ticket_type_id, i.quantity) for i in out.items} == {
        (free_type.id, 1),
        (other_type.id, 2),
    }
    names_by_type: dict[uuid.UUID, set[str]] = {}
    for ticket in out.tickets:
        names_by_type.setdefault(ticket.ticket_type_id, set()).add(ticket.attendee.name)
    assert names_by_type == {free_type.id: {"Aruzhan"}, other_type.id: {"Dias", "Saule"}}
    dias = next(t for t in out.tickets if t.attendee.name == "Dias")
    assert dias.attendee.email is None


def test_attendees_are_linked_to_their_order_item(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    other_type = create_free_ticket_type(db_session, event, name="Student")

    out = register(db_session, buyer, event, registration((free_type, 1), (other_type, 2)))

    db_session.expire_all()
    tickets = db_session.scalars(
        select(Ticket).where(Ticket.id.in_([t.id for t in out.tickets]))
    ).all()
    assert len(tickets) == 3
    # The stored link (needed to issue paid tickets later) matches the ticket's item.
    assert all(t.attendee.order_item_id == t.order_item_id for t in tickets)


def test_locks_event_then_ticket_types_before_counting(
    db_session: Session,
    buyer: User,
    event: Event,
    free_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event.capacity = 100
    calls: list[str] = []

    def spy(name: str) -> None:
        real = getattr(repository, name)

        def wrapper(*args: object, **kwargs: object) -> object:
            calls.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(repository, name, wrapper)

    for name in ("lock_event", "lock_ticket_types", "sold_quantities", "held_quantity_for_event"):
        spy(name)

    register(db_session, buyer, event, registration((free_type, 1)))

    assert calls == [
        "lock_event",
        "lock_ticket_types",
        "sold_quantities",
        "held_quantity_for_event",
    ]


# --- event rules ---


@pytest.mark.parametrize(
    "status", [EventStatus.DRAFT, EventStatus.CANCELLED, EventStatus.COMPLETED]
)
def test_unpublished_event_not_found(db_session: Session, buyer: User, status: EventStatus) -> None:
    event = create_event(db_session, create_organizer(db_session), status=status)
    ticket_type = create_free_ticket_type(db_session, event)

    with pytest.raises(NotFoundError, match="Event not found"):
        register(db_session, buyer, event, registration((ticket_type, 1)))


def test_private_event_not_found(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.visibility = EventVisibility.PRIVATE
    with pytest.raises(NotFoundError, match="Event not found"):
        register(db_session, buyer, event, registration((free_type, 1)))


def test_unlisted_event_allowed(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.visibility = EventVisibility.UNLISTED
    assert register(db_session, buyer, event, registration((free_type, 1))).tickets


def test_assigned_seating_event_rejected(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.seating_type = SeatingType.ASSIGNED
    with pytest.raises(BusinessRuleError, match="This event uses seat selection"):
        register(db_session, buyer, event, registration((free_type, 1)))


def test_unknown_event_not_found(db_session: Session, buyer: User, free_type: TicketType) -> None:
    with pytest.raises(NotFoundError, match="Event not found"):
        service.register_free(
            db_session, buyer, uuid.uuid4(), registration((free_type, 1)), now=NOW
        )


def test_started_event_rejected(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.start_at = NOW
    with pytest.raises(BusinessRuleError, match="already started"):
        register(db_session, buyer, event, registration((free_type, 1)))


def test_registration_not_open_yet(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.registration_opens_at = NOW + HOUR
    with pytest.raises(BusinessRuleError, match="not opened"):
        register(db_session, buyer, event, registration((free_type, 1)))


def test_registration_closed(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.registration_closes_at = NOW
    with pytest.raises(BusinessRuleError, match="Registration is closed"):
        register(db_session, buyer, event, registration((free_type, 1)))


def test_inside_registration_window_ok(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    event.registration_opens_at = NOW
    event.registration_closes_at = NOW + HOUR
    assert register(db_session, buyer, event, registration((free_type, 1))).tickets


# --- ticket type rules ---


def test_paid_ticket_type_rejected(db_session: Session, buyer: User, event: Event) -> None:
    paid = create_ticket_type(db_session, event)
    with pytest.raises(BusinessRuleError, match="Use checkout"):
        register(db_session, buyer, event, registration((paid, 1)))


@pytest.mark.parametrize("kind_overrides", [{}, {"kind": "paid", "price_kzt": 5000}])
def test_hidden_ticket_type_not_found(
    db_session: Session, buyer: User, event: Event, kind_overrides: dict
) -> None:
    hidden = create_free_ticket_type(db_session, event, is_hidden=True, **kind_overrides)
    with pytest.raises(NotFoundError, match="Ticket type not found"):
        register(db_session, buyer, event, registration((hidden, 1)))


def test_ticket_type_of_other_event_not_found(
    db_session: Session, buyer: User, event: Event
) -> None:
    other_event = create_event(db_session, create_organizer(db_session))
    foreign = create_free_ticket_type(db_session, other_event)
    with pytest.raises(NotFoundError, match="Ticket type not found"):
        register(db_session, buyer, event, registration((foreign, 1)))


def test_unknown_ticket_type_not_found(db_session: Session, buyer: User, event: Event) -> None:
    data = RegistrationCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(uuid.uuid4()), "quantity": 1}],
            "attendees": [{"name": "Aruzhan"}],
        }
    )
    with pytest.raises(NotFoundError, match="Ticket type not found"):
        register(db_session, buyer, event, data)


@pytest.mark.parametrize(
    ("window", "message"),
    [
        ({"sales_start_at": NOW + HOUR}, "not started"),
        ({"sales_end_at": NOW}, "ended"),
        ({"sales_start_at": NOW - 2 * HOUR, "sales_end_at": NOW - HOUR}, "ended"),
    ],
)
def test_outside_sales_window_rejected(
    db_session: Session, buyer: User, event: Event, window: dict, message: str
) -> None:
    ticket_type = create_free_ticket_type(db_session, event, **window)
    with pytest.raises(BusinessRuleError, match=message):
        register(db_session, buyer, event, registration((ticket_type, 1)))


def test_sales_window_start_is_inclusive(db_session: Session, buyer: User, event: Event) -> None:
    ticket_type = create_free_ticket_type(
        db_session, event, sales_start_at=NOW, sales_end_at=NOW + HOUR
    )
    assert register(db_session, buyer, event, registration((ticket_type, 1))).tickets


def test_quantity_above_max_per_order_rejected(
    db_session: Session, buyer: User, event: Event
) -> None:
    ticket_type = create_free_ticket_type(db_session, event, max_per_order=2)
    with pytest.raises(BusinessRuleError, match="At most 2"):
        register(db_session, buyer, event, registration((ticket_type, 3)))


def test_no_max_per_order_allows_large_quantity(
    db_session: Session, buyer: User, event: Event
) -> None:
    ticket_type = create_free_ticket_type(db_session, event, max_per_order=None)
    assert len(register(db_session, buyer, event, registration((ticket_type, 15))).tickets) == 15


# --- availability ---


def test_not_enough_tickets_left(db_session: Session, buyer: User, event: Event) -> None:
    ticket_type = create_free_ticket_type(db_session, event, quantity_total=5)
    create_order_item(db_session, ticket_type, 4, status=OrderStatus.FREE_CONFIRMED)

    with pytest.raises(BusinessRuleError, match="Not enough tickets left"):
        register(db_session, buyer, event, registration((ticket_type, 2)))

    assert len(register(db_session, buyer, event, registration((ticket_type, 1))).tickets) == 1


def test_sold_out_rejected(db_session: Session, buyer: User, event: Event) -> None:
    ticket_type = create_free_ticket_type(db_session, event, quantity_total=1)
    register(db_session, buyer, event, registration((ticket_type, 1)))

    with pytest.raises(BusinessRuleError, match="Not enough tickets left"):
        register(db_session, buyer, event, registration((ticket_type, 1)))


@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.REFUNDED])
def test_released_orders_do_not_count(
    db_session: Session, buyer: User, event: Event, status: OrderStatus
) -> None:
    ticket_type = create_free_ticket_type(db_session, event, quantity_total=2)
    create_order_item(db_session, ticket_type, 2, status=status)

    assert len(register(db_session, buyer, event, registration((ticket_type, 2))).tickets) == 2


def test_failing_item_creates_nothing(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    sold_out = create_free_ticket_type(db_session, event, name="VIP", quantity_total=1)
    create_order_item(db_session, sold_out, 1, status=OrderStatus.FREE_CONFIRMED)

    with pytest.raises(BusinessRuleError, match="VIP"):
        register(db_session, buyer, event, registration((free_type, 1), (sold_out, 1)))

    assert db_session.scalar(select(func.count()).where(Order.buyer_user_id == buyer.id)) == 0


# --- event capacity ---


def test_capacity_counts_all_ticket_types(db_session: Session, buyer: User, event: Event) -> None:
    event.capacity = 3
    standard = create_free_ticket_type(db_session, event, name="Standard")
    student = create_free_ticket_type(db_session, event, name="Student")
    create_order_item(db_session, standard, 2, status=OrderStatus.FREE_CONFIRMED)

    with pytest.raises(BusinessRuleError, match="Not enough capacity left"):
        register(db_session, buyer, event, registration((student, 2)))

    # Exactly filling the capacity is fine; after that the event is full.
    assert len(register(db_session, buyer, event, registration((student, 1))).tickets) == 1
    with pytest.raises(BusinessRuleError, match="Not enough capacity left"):
        register(db_session, buyer, event, registration((standard, 1)))


def test_capacity_counts_the_whole_request(db_session: Session, buyer: User, event: Event) -> None:
    event.capacity = 2
    standard = create_free_ticket_type(db_session, event, name="Standard")
    student = create_free_ticket_type(db_session, event, name="Student")

    with pytest.raises(BusinessRuleError, match="Not enough capacity left"):
        register(db_session, buyer, event, registration((standard, 2), (student, 1)))


def test_no_capacity_means_unlimited(db_session: Session, buyer: User, event: Event) -> None:
    assert event.capacity is None
    ticket_type = create_free_ticket_type(db_session, event, quantity_total=1000)
    create_order_item(db_session, ticket_type, 900, status=OrderStatus.FREE_CONFIRMED)

    assert len(register(db_session, buyer, event, registration((ticket_type, 10))).tickets) == 10


@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.REFUNDED])
def test_released_orders_do_not_count_toward_capacity(
    db_session: Session, buyer: User, event: Event, status: OrderStatus
) -> None:
    event.capacity = 2
    ticket_type = create_free_ticket_type(db_session, event)
    create_order_item(db_session, ticket_type, 2, status=status)

    assert len(register(db_session, buyer, event, registration((ticket_type, 2))).tickets) == 2


def test_cancelled_event_seen_on_locked_row(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    """The session's copy says published, but the row was cancelled: the lock re-reads it."""
    db_session.execute(
        update(Event).where(Event.id == event.id).values(status=EventStatus.CANCELLED),
        execution_options={"synchronize_session": False},
    )
    assert event.status == EventStatus.PUBLISHED  # stale copy in the session

    with pytest.raises(NotFoundError, match="Event not found"):
        register(db_session, buyer, event, registration((free_type, 1)))


# --- buyer reads ---


def test_get_my_order_matches_registration_response(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    other_type = create_free_ticket_type(db_session, event, name="Student")
    created = register(db_session, buyer, event, registration((free_type, 2), (other_type, 1)))
    db_session.expire_all()

    assert service.get_my_order(db_session, buyer, created.id) == created


def test_get_order_of_other_user_not_found(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    created = register(db_session, buyer, event, registration((free_type, 1)))

    with pytest.raises(NotFoundError, match="Order not found"):
        service.get_my_order(db_session, create_user(db_session), created.id)


def test_get_unknown_order_not_found(db_session: Session, buyer: User) -> None:
    with pytest.raises(NotFoundError, match="Order not found"):
        service.get_my_order(db_session, buyer, uuid.uuid4())


def test_list_my_orders_returns_only_mine(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    mine = [register(db_session, buyer, event, registration((free_type, 1))) for _ in range(2)]
    register(db_session, create_user(db_session), event, registration((free_type, 1)))
    db_session.expire_all()

    orders = service.list_my_orders(db_session, buyer)

    assert sorted(orders, key=lambda o: o.id) == sorted(mine, key=lambda o: o.id)
    assert service.list_my_orders(db_session, create_user(db_session)) == []


def test_get_my_ticket_returns_token(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    [created] = register(db_session, buyer, event, registration((free_type, 1))).tickets
    db_session.expire_all()

    assert service.get_my_ticket(db_session, buyer, created.id) == created


def test_get_ticket_of_other_user_not_found(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    [created] = register(db_session, buyer, event, registration((free_type, 1))).tickets

    with pytest.raises(NotFoundError, match="Ticket not found"):
        service.get_my_ticket(db_session, create_user(db_session), created.id)


def test_get_unknown_ticket_not_found(db_session: Session, buyer: User) -> None:
    with pytest.raises(NotFoundError, match="Ticket not found"):
        service.get_my_ticket(db_session, buyer, uuid.uuid4())


# --- concurrency ---
# These run on separate real connections, so rows are committed and deleted afterwards.

RACERS = 5


@dataclass
class Committed:
    event: Event
    ticket_types: list[TicketType]
    buyer_ids: list[uuid.UUID]


@pytest.fixture
def committed() -> Iterator[Callable[..., Committed]]:
    """Builds a committed published event with free ticket types and buyers; cleans up."""
    created: list[tuple[Committed, OrganizerProfile]] = []

    def make(quantity_total: int, types: int = 1, capacity: int | None = None) -> Committed:
        with SessionLocal() as setup:
            organizer = create_organizer(setup)
            event = create_event(setup, organizer)
            event.capacity = capacity
            ticket_types = [
                create_free_ticket_type(setup, event, name=f"T{n}", quantity_total=quantity_total)
                for n in range(types)
            ]
            buyers = [create_user(setup) for _ in range(RACERS)]
            setup.commit()
        result = Committed(event, ticket_types, [b.id for b in buyers])
        created.append((result, organizer))
        return result

    yield make

    with SessionLocal() as cleanup:
        for c, organizer in created:
            order_ids = select(Order.id).where(Order.event_id == c.event.id)
            item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
            cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
            cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
            cleanup.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            cleanup.execute(delete(Order).where(Order.event_id == c.event.id))
            cleanup.execute(delete(TicketType).where(TicketType.event_id == c.event.id))
            cleanup.execute(delete(Event).where(Event.id == c.event.id))
            cleanup.execute(delete(Venue).where(Venue.id == c.event.venue_id))
            cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == organizer.id))
            cleanup.execute(delete(User).where(User.id.in_([organizer.user_id, *c.buyer_ids])))
        cleanup.commit()


def _register_in_own_session(
    buyer_id: uuid.UUID, event_id: uuid.UUID, ticket_type: TicketType, before=None
) -> str:
    with SessionLocal() as session:
        buyer = session.get_one(User, buyer_id)
        if before:
            before()
        try:
            service.register_free(session, buyer, event_id, registration((ticket_type, 1)))
        except BusinessRuleError:
            return "rejected"
        except NotFoundError:
            return "not found"
        return "won"


def _race(c: Committed, ticket_type_for: Callable[[int], TicketType]) -> list[str]:
    """Every buyer registers for one ticket at the same moment; returns sorted outcomes."""
    barrier = threading.Barrier(RACERS)
    results: list[str] = []

    def run(n: int) -> None:
        outcome = _register_in_own_session(
            c.buyer_ids[n], c.event.id, ticket_type_for(n), before=barrier.wait
        )
        results.append(outcome)  # list.append is atomic

    threads = [threading.Thread(target=run, args=(n,)) for n in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return sorted(results)


def test_concurrent_registrations_cannot_oversell(committed: Callable[..., Committed]) -> None:
    """Several buyers race for the last ticket on separate connections; exactly one wins."""
    c = committed(quantity_total=1)
    [ticket_type] = c.ticket_types

    assert _race(c, lambda n: ticket_type) == ["rejected"] * (RACERS - 1) + ["won"]
    with SessionLocal() as check:
        sold = repository.sold_quantities(check, [ticket_type.id], datetime.now(UTC))
    assert sold == {ticket_type.id: 1}


def test_concurrent_registrations_cannot_exceed_capacity(
    committed: Callable[..., Committed],
) -> None:
    """Racers use two different ticket types, so only the event lock serializes them."""
    c = committed(quantity_total=RACERS, types=2, capacity=1)

    outcomes = _race(c, lambda n: c.ticket_types[n % 2])

    assert outcomes == ["rejected"] * (RACERS - 1) + ["won"]
    with SessionLocal() as check:
        held = repository.held_quantity_for_event(check, c.event.id, datetime.now(UTC))
    assert held == 1


def _wait_for_lock_waiter(timeout: float = 10) -> None:
    """Block until some connection to this database is waiting on a row lock."""
    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            if waiting:
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError("registration never waited for the event lock")


def test_registration_after_concurrent_cancel_is_not_found(
    committed: Callable[..., Committed],
) -> None:
    """A cancel holds the event lock; the registration waits, then sees the cancel and 404s."""
    c = committed(quantity_total=10)
    outcome: list[str] = []

    with SessionLocal() as canceller:
        event = repository.lock_event(canceller, c.event.id)
        assert event is not None
        event.status = EventStatus.CANCELLED
        canceller.flush()

        registrant = threading.Thread(
            target=lambda: outcome.append(
                _register_in_own_session(c.buyer_ids[0], c.event.id, c.ticket_types[0])
            )
        )
        registrant.start()
        _wait_for_lock_waiter()
        canceller.commit()

    registrant.join(timeout=30)
    assert outcome == ["not found"]
