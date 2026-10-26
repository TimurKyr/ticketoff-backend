import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile
from app.modules.orders import service
from app.modules.orders.enums import OrderStatus, TicketKind
from app.modules.orders.schemas import TicketTypeCreate, TicketTypeUpdate
from tests.factories import (
    create_event,
    create_order_item,
    create_organizer,
    create_ticket_type,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    return create_event(db_session, organizer)


def paid_create(**overrides: object) -> TicketTypeCreate:
    fields: dict[str, object] = {
        "name": "General",
        "kind": TicketKind.PAID,
        "price_kzt": 5000,
        "quantity_total": 100,
    }
    return TicketTypeCreate(**(fields | overrides))


# --- create ---


def test_create_returns_ticket_type_with_inventory(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    out = service.create_ticket_type(db_session, organizer.id, event.id, paid_create())

    assert out.event_id == event.id
    assert out.kind == TicketKind.PAID
    assert out.max_per_order == 10
    assert (out.sold, out.available) == (0, 100)


def test_create_free_ticket_type_defaults(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    data = TicketTypeCreate(name="Free entry", quantity_total=50)

    out = service.create_ticket_type(db_session, organizer.id, event.id, data)

    assert (out.kind, out.price_kzt) == (TicketKind.FREE, 0)


def test_create_keeps_unlimited_max_per_order(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    data = paid_create(max_per_order=None)

    out = service.create_ticket_type(db_session, organizer.id, event.id, data)

    assert out.max_per_order is None


def test_create_on_unknown_event_raises_not_found(
    db_session: Session, organizer: OrganizerProfile
) -> None:
    with pytest.raises(NotFoundError):
        service.create_ticket_type(db_session, organizer.id, uuid.uuid4(), paid_create())


def test_create_on_other_organizers_event_is_denied(db_session: Session, event: Event) -> None:
    stranger = create_organizer(db_session)

    with pytest.raises(PermissionDeniedError):
        service.create_ticket_type(db_session, stranger.id, event.id, paid_create())


@pytest.mark.parametrize("status", [EventStatus.CANCELLED, EventStatus.COMPLETED])
def test_create_on_closed_event_is_rejected(
    db_session: Session, organizer: OrganizerProfile, status: EventStatus
) -> None:
    closed = create_event(db_session, organizer, status=status)

    with pytest.raises(BusinessRuleError):
        service.create_ticket_type(db_session, organizer.id, closed.id, paid_create())


def test_create_on_draft_event_is_allowed(db_session: Session, organizer: OrganizerProfile) -> None:
    draft = create_event(db_session, organizer, status=EventStatus.DRAFT)

    out = service.create_ticket_type(db_session, organizer.id, draft.id, paid_create())

    assert out.event_id == draft.id


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": TicketKind.FREE, "price_kzt": 100},
        {"kind": TicketKind.PAID, "price_kzt": 0},
        {"price_kzt": -1},
        {"quantity_total": 0},
        {"max_per_order": 0},
        {"quantity_total": 5, "max_per_order": 6},
        {"sales_start_at": NOW, "sales_end_at": NOW},
        {"sales_start_at": NOW, "sales_end_at": NOW - timedelta(hours=1)},
        {"name": ""},
    ],
)
def test_create_schema_rejects_invalid_input(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        paid_create(**fields)


def test_create_schema_allows_unlimited_max_per_order() -> None:
    assert paid_create(max_per_order=None).max_per_order is None


# --- list (organizer) ---


def test_list_includes_hidden_and_inventory(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    visible = create_ticket_type(db_session, event, name="A")
    hidden = create_ticket_type(db_session, event, name="B", is_hidden=True)
    create_order_item(db_session, visible, quantity=3)

    result = {tt.id: tt for tt in service.list_ticket_types(db_session, organizer.id, event.id)}

    assert set(result) == {visible.id, hidden.id}
    assert (result[visible.id].sold, result[visible.id].available) == (3, 97)


def test_list_on_other_organizers_event_is_denied(db_session: Session, event: Event) -> None:
    stranger = create_organizer(db_session)

    with pytest.raises(PermissionDeniedError):
        service.list_ticket_types(db_session, stranger.id, event.id)


# --- availability ---


def test_availability_counts_only_inventory_holding_orders(
    db_session: Session, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, quantity_total=100)
    create_order_item(db_session, ticket_type, 1, OrderStatus.PENDING)
    create_order_item(db_session, ticket_type, 2, OrderStatus.PAID)
    create_order_item(db_session, ticket_type, 4, OrderStatus.FREE_CONFIRMED)
    create_order_item(db_session, ticket_type, 8, OrderStatus.CANCELLED)
    create_order_item(db_session, ticket_type, 16, OrderStatus.REFUNDED)

    assert service.get_availability(db_session, [ticket_type]) == {ticket_type.id: 93}


def test_availability_is_never_negative(db_session: Session, event: Event) -> None:
    ticket_type = create_ticket_type(db_session, event, quantity_total=5, max_per_order=5)
    create_order_item(db_session, ticket_type, 7)

    assert service.get_availability(db_session, [ticket_type]) == {ticket_type.id: 0}


# --- update ---


def test_update_applies_only_sent_fields(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, description="Keep me")

    out = service.update_ticket_type(
        db_session, organizer.id, ticket_type.id, TicketTypeUpdate(name="VIP")
    )

    assert (out.name, out.description, out.price_kzt) == ("VIP", "Keep me", 5000)


def test_update_hide_and_unhide(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event)

    hidden = service.update_ticket_type(
        db_session, organizer.id, ticket_type.id, TicketTypeUpdate(is_hidden=True)
    )
    shown = service.update_ticket_type(
        db_session, organizer.id, ticket_type.id, TicketTypeUpdate(is_hidden=False)
    )

    assert (hidden.is_hidden, shown.is_hidden) == (True, False)


def test_update_unknown_ticket_type_raises_not_found(
    db_session: Session, organizer: OrganizerProfile
) -> None:
    with pytest.raises(NotFoundError):
        service.update_ticket_type(
            db_session, organizer.id, uuid.uuid4(), TicketTypeUpdate(name="X")
        )


def test_update_other_organizers_ticket_type_is_denied(db_session: Session, event: Event) -> None:
    ticket_type = create_ticket_type(db_session, event)
    stranger = create_organizer(db_session)

    with pytest.raises(PermissionDeniedError):
        service.update_ticket_type(
            db_session, stranger.id, ticket_type.id, TicketTypeUpdate(name="X")
        )


@pytest.mark.parametrize("status", [EventStatus.CANCELLED, EventStatus.COMPLETED])
def test_update_on_closed_event_is_rejected(
    db_session: Session, organizer: OrganizerProfile, status: EventStatus
) -> None:
    closed = create_event(db_session, organizer, status=status)
    ticket_type = create_ticket_type(db_session, closed)

    with pytest.raises(BusinessRuleError):
        service.update_ticket_type(
            db_session, organizer.id, ticket_type.id, TicketTypeUpdate(is_hidden=True)
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": TicketKind.FREE},  # paid price 5000 stays -> mismatch
        {"price_kzt": 0},  # still paid
        {"max_per_order": 101},  # above quantity_total 100
        {"quantity_total": 5},  # below max_per_order 10
        {"sales_end_at": NOW},  # before existing start below
    ],
)
def test_update_checks_rules_on_merged_values(
    db_session: Session, organizer: OrganizerProfile, event: Event, fields: dict[str, object]
) -> None:
    ticket_type = create_ticket_type(db_session, event, sales_start_at=NOW + timedelta(days=1))

    with pytest.raises(BusinessRuleError):
        service.update_ticket_type(
            db_session, organizer.id, ticket_type.id, TicketTypeUpdate(**fields)
        )


def test_update_switch_kind_with_matching_price_is_allowed(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event)

    out = service.update_ticket_type(
        db_session,
        organizer.id,
        ticket_type.id,
        TicketTypeUpdate(kind=TicketKind.FREE, price_kzt=0),
    )

    assert (out.kind, out.price_kzt) == (TicketKind.FREE, 0)


def test_update_kind_is_locked_after_sales(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event)
    create_order_item(db_session, ticket_type, 1)

    with pytest.raises(BusinessRuleError, match="kind"):
        service.update_ticket_type(
            db_session,
            organizer.id,
            ticket_type.id,
            TicketTypeUpdate(kind=TicketKind.FREE, price_kzt=0),
        )


def test_update_price_is_allowed_after_sales(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event)
    item = create_order_item(db_session, ticket_type, 1)

    out = service.update_ticket_type(
        db_session, organizer.id, ticket_type.id, TicketTypeUpdate(price_kzt=7000)
    )

    assert out.price_kzt == 7000
    assert item.unit_price_kzt == 5000


def test_update_quantity_below_sold_is_rejected(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, max_per_order=None)
    create_order_item(db_session, ticket_type, 30)

    with pytest.raises(BusinessRuleError, match="sold"):
        service.update_ticket_type(
            db_session, organizer.id, ticket_type.id, TicketTypeUpdate(quantity_total=29)
        )


def test_update_quantity_equal_to_sold_is_allowed(
    db_session: Session, organizer: OrganizerProfile, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, max_per_order=None)
    create_order_item(db_session, ticket_type, 30)

    out = service.update_ticket_type(
        db_session, organizer.id, ticket_type.id, TicketTypeUpdate(quantity_total=30)
    )

    assert (out.sold, out.available) == (30, 0)


@pytest.mark.parametrize("field", ["name", "kind", "price_kzt", "quantity_total", "is_hidden"])
def test_update_schema_rejects_null_for_required_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        TicketTypeUpdate(**{field: None})


def test_update_schema_allows_clearing_optional_fields() -> None:
    data = TicketTypeUpdate(description=None, max_per_order=None, sales_end_at=None)

    assert data.model_dump(exclude_unset=True) == {
        "description": None,
        "max_per_order": None,
        "sales_end_at": None,
    }


# --- public list ---


def test_public_list_shows_only_visible_types_on_sale(db_session: Session, event: Event) -> None:
    open_ended = create_ticket_type(db_session, event, name="Open")
    in_window = create_ticket_type(
        db_session,
        event,
        name="Window",
        sales_start_at=NOW - timedelta(days=1),
        sales_end_at=NOW + timedelta(days=1),
    )
    create_ticket_type(db_session, event, name="Hidden", is_hidden=True)
    create_ticket_type(db_session, event, name="Future", sales_start_at=NOW + timedelta(hours=1))
    create_ticket_type(db_session, event, name="Ended", sales_end_at=NOW)

    result = service.list_public_ticket_types(db_session, event.id, now=NOW)

    assert {tt.id for tt in result} == {open_ended.id, in_window.id}


def test_public_list_includes_sold_out_with_zero_available(
    db_session: Session, event: Event
) -> None:
    ticket_type = create_ticket_type(db_session, event, quantity_total=2, max_per_order=2)
    create_order_item(db_session, ticket_type, 2)

    [out] = service.list_public_ticket_types(db_session, event.id, now=NOW)

    assert out.available == 0


@pytest.mark.parametrize(
    "status", [EventStatus.DRAFT, EventStatus.CANCELLED, EventStatus.COMPLETED]
)
def test_public_list_hides_unpublished_events(
    db_session: Session, organizer: OrganizerProfile, status: EventStatus
) -> None:
    unpublished = create_event(db_session, organizer, status=status)
    create_ticket_type(db_session, unpublished)

    with pytest.raises(NotFoundError):
        service.list_public_ticket_types(db_session, unpublished.id, now=NOW)


def test_public_list_unknown_event_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        service.list_public_ticket_types(db_session, uuid.uuid4(), now=NOW)
