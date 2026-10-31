import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.db import SessionLocal
from app.modules.events.enums import EventStatus
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
        claims = verify_ticket_token(ticket.qr_token)
        assert claims is not None
        assert (claims.ticket_id, claims.event_id) == (ticket.id, event.id)


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


def test_attendees_default_to_buyer_email(
    db_session: Session, buyer: User, event: Event, free_type: TicketType
) -> None:
    out = register(db_session, buyer, event, registration((free_type, 2)))

    assert [(t.attendee.name, t.attendee.email) for t in out.tickets] == [
        (buyer.email, buyer.email)
    ] * 2


def test_locks_ticket_types_before_counting_sold(
    db_session: Session,
    buyer: User,
    event: Event,
    free_type: TicketType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real_lock, real_sold = repository.lock_ticket_types, repository.sold_quantities

    def lock(session: Session, ids: list[uuid.UUID]) -> list[TicketType]:
        calls.append("lock")
        return real_lock(session, ids)

    def sold(*args: object) -> dict[uuid.UUID, int]:
        calls.append("sold")
        return real_sold(*args)

    monkeypatch.setattr(repository, "lock_ticket_types", lock)
    monkeypatch.setattr(repository, "sold_quantities", sold)

    register(db_session, buyer, event, registration((free_type, 1)))

    assert calls == ["lock", "sold"]


# --- event rules ---


@pytest.mark.parametrize(
    "status", [EventStatus.DRAFT, EventStatus.CANCELLED, EventStatus.COMPLETED]
)
def test_unpublished_event_not_found(db_session: Session, buyer: User, status: EventStatus) -> None:
    event = create_event(db_session, create_organizer(db_session), status=status)
    ticket_type = create_free_ticket_type(db_session, event)

    with pytest.raises(NotFoundError, match="Event not found"):
        register(db_session, buyer, event, registration((ticket_type, 1)))


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
        {"items": [{"ticket_type_id": str(uuid.uuid4()), "quantity": 1}]}
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

    assert orders == sorted(mine, key=lambda o: o.id)
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


def test_concurrent_registrations_cannot_oversell() -> None:
    """Several buyers race for the last ticket on separate connections; exactly one wins."""
    racers = 5
    with SessionLocal() as setup:
        organizer = create_organizer(setup)
        event = create_event(setup, organizer)
        ticket_type = create_free_ticket_type(setup, event, quantity_total=1)
        buyers = [create_user(setup) for _ in range(racers)]
        setup.commit()

    barrier = threading.Barrier(racers)
    results: list[str] = []
    lock = threading.Lock()

    def race(buyer_id: uuid.UUID) -> None:
        with SessionLocal() as session:
            buyer = session.get_one(User, buyer_id)
            barrier.wait()
            try:
                service.register_free(session, buyer, event.id, registration((ticket_type, 1)))
                outcome = "won"
            except BusinessRuleError:
                outcome = "sold out"
        with lock:
            results.append(outcome)

    try:
        threads = [threading.Thread(target=race, args=(b.id,)) for b in buyers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results) == ["sold out"] * (racers - 1) + ["won"]
        with SessionLocal() as check:
            sold = repository.sold_quantities(
                check, [ticket_type.id], service.INVENTORY_HOLDING_STATUSES
            )
            assert sold == {ticket_type.id: 1}
    finally:
        with SessionLocal() as cleanup:
            order_ids = select(Order.id).where(Order.event_id == event.id)
            item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
            cleanup.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
            cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
            cleanup.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            cleanup.execute(delete(Order).where(Order.event_id == event.id))
            cleanup.execute(delete(TicketType).where(TicketType.id == ticket_type.id))
            cleanup.execute(delete(Event).where(Event.id == event.id))
            cleanup.execute(delete(Venue).where(Venue.id == event.venue_id))
            cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == organizer.id))
            user_ids = [organizer.user_id, *(b.id for b in buyers)]
            cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            cleanup.commit()
