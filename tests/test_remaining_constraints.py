"""Database rules added in BE-10: they must hold even if service code has a bug."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.events.models import Event, Row, Seat, VenueSection
from app.modules.ops.models import CheckInRecord
from app.modules.orders.models import Attendee, Order, Ticket
from app.modules.payments.enums import PaymentPurpose
from app.modules.payments.models import Payment
from app.modules.promotions.enums import DiscountType
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
from app.modules.seating.enums import SeatHoldStatus
from app.modules.seating.models import SeatHold
from tests.factories import (
    create_event,
    create_order_item,
    create_organizer,
    create_ticket_type,
    create_user,
)

HELD_UNTIL = datetime.now(UTC) + timedelta(minutes=10)


def create_seat(session: Session, event: Event) -> Seat:
    section = VenueSection(venue_id=event.venue_id, name="Parterre")
    session.add(section)
    session.flush()
    row = Row(section_id=section.id, label="A")
    session.add(row)
    session.flush()
    seat = Seat(row_id=row.id, number="1")
    session.add(seat)
    session.flush()
    return seat


def create_order(session: Session, event: Event) -> Order:
    order = Order(buyer_user_id=create_user(session).id, event_id=event.id)
    session.add(order)
    session.flush()
    return order


def add_hold(session: Session, seat: Seat, event: Event, status: SeatHoldStatus) -> SeatHold:
    hold = SeatHold(seat_id=seat.id, event_id=event.id, held_until=HELD_UNTIL, status=status)
    session.add(hold)
    session.flush()
    return hold


def test_seat_cannot_have_two_active_holds(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    seat = create_seat(db_session, event)
    add_hold(db_session, seat, event, SeatHoldStatus.HELD)

    with pytest.raises(IntegrityError, match="uq_seat_holds_active_seat"):
        add_hold(db_session, seat, event, SeatHoldStatus.HELD)


def test_sold_seat_cannot_be_held_again(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    seat = create_seat(db_session, event)
    add_hold(db_session, seat, event, SeatHoldStatus.CONVERTED)

    with pytest.raises(IntegrityError, match="uq_seat_holds_active_seat"):
        add_hold(db_session, seat, event, SeatHoldStatus.HELD)


def test_released_hold_does_not_block_new_hold(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    seat = create_seat(db_session, event)
    add_hold(db_session, seat, event, SeatHoldStatus.RELEASED)

    add_hold(db_session, seat, event, SeatHoldStatus.HELD)  # must not raise


def test_refunded_seat_returns_to_sale(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    seat = create_seat(db_session, event)
    sold = add_hold(db_session, seat, event, SeatHoldStatus.CONVERTED)
    sold.status = SeatHoldStatus.RELEASED  # what the refund flow must do
    db_session.flush()

    add_hold(db_session, seat, event, SeatHoldStatus.HELD)  # must not raise


@pytest.mark.parametrize("purpose", [PaymentPurpose.TICKET_ORDER, PaymentPurpose.ACTIVATION])
def test_database_accepts_valid_payment(db_session: Session, purpose: PaymentPurpose) -> None:
    event = create_event(db_session, create_organizer(db_session))
    is_ticket = purpose is PaymentPurpose.TICKET_ORDER
    db_session.add(
        Payment(
            purpose=purpose,
            order_id=create_order(db_session, event).id if is_ticket else None,
            event_id=None if is_ticket else event.id,
            amount_kzt=1000,
        )
    )
    db_session.flush()  # must not raise


@pytest.mark.parametrize(
    ("purpose", "with_order", "with_event", "amount", "constraint"),
    [
        (PaymentPurpose.TICKET_ORDER, False, False, 1000, "ticket_order_links_order_only"),
        (PaymentPurpose.TICKET_ORDER, True, True, 1000, "ticket_order_links_order_only"),
        (PaymentPurpose.ACTIVATION, False, False, 1000, "activation_links_event_only"),
        (PaymentPurpose.ACTIVATION, True, True, 1000, "activation_links_event_only"),
        (PaymentPurpose.ACTIVATION, False, True, -1, "amount_non_negative"),
    ],
)
def test_database_rejects_invalid_payment(
    db_session: Session,
    purpose: PaymentPurpose,
    with_order: bool,
    with_event: bool,
    amount: int,
    constraint: str,
) -> None:
    event = create_event(db_session, create_organizer(db_session))
    payment = Payment(
        purpose=purpose,
        order_id=create_order(db_session, event).id if with_order else None,
        event_id=event.id if with_event else None,
        amount_kzt=amount,
    )
    db_session.add(payment)

    with pytest.raises(IntegrityError, match=f"ck_payments_{constraint}"):
        db_session.flush()


def test_order_can_redeem_only_one_promo_code(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    campaign = PromotionalCampaign(
        event_id=event.id,
        name="Early bird",
        discount_type=DiscountType.PERCENT,
        discount_value=10,
    )
    db_session.add(campaign)
    db_session.flush()
    codes = [
        PromoCode(campaign_id=campaign.id, code=f"EARLY{i}", qr_token=f"qr-early-{i}")
        for i in range(2)
    ]
    db_session.add_all(codes)
    db_session.flush()
    order = create_order(db_session, event)
    db_session.add(PromoRedemption(promo_code_id=codes[0].id, order_id=order.id, discount_kzt=500))
    db_session.flush()

    db_session.add(PromoRedemption(promo_code_id=codes[1].id, order_id=order.id, discount_kzt=500))
    with pytest.raises(IntegrityError, match="uq_promo_redemptions_order_id"):
        db_session.flush()


def create_ticket(session: Session) -> Ticket:
    event = create_event(session, create_organizer(session))
    item = create_order_item(session, create_ticket_type(session, event), quantity=1)
    attendee = Attendee(order_id=item.order_id, name="Guest")
    session.add(attendee)
    session.flush()
    ticket = Ticket(order_item_id=item.id, attendee_id=attendee.id, qr_token=f"qr-{item.id}")
    session.add(ticket)
    session.flush()
    return ticket


def check_in(session: Session, ticket: Ticket, event_admin_id: uuid.UUID) -> CheckInRecord:
    record = CheckInRecord(ticket_id=ticket.id, event_admin_id=event_admin_id)
    session.add(record)
    session.flush()
    return record


def test_ticket_cannot_be_checked_in_twice(db_session: Session) -> None:
    ticket = create_ticket(db_session)
    scanner = create_user(db_session)
    check_in(db_session, ticket, scanner.id)

    with pytest.raises(IntegrityError, match="uq_check_in_records_active_ticket"):
        check_in(db_session, ticket, scanner.id)


def test_reversed_check_in_allows_new_check_in(db_session: Session) -> None:
    ticket = create_ticket(db_session)
    scanner = create_user(db_session)
    first = check_in(db_session, ticket, scanner.id)
    first.is_reversed = True
    db_session.flush()

    check_in(db_session, ticket, scanner.id)  # must not raise


def test_check_in_is_reversed_defaults_in_database(db_session: Session) -> None:
    ticket = create_ticket(db_session)
    scanner = create_user(db_session)
    record_id = uuid.uuid4()
    # Raw SQL that leaves out is_reversed: the database default must fill in false.
    db_session.execute(
        text(
            "INSERT INTO check_in_records (id, ticket_id, event_admin_id) "
            "VALUES (:id, :ticket_id, :admin_id)"
        ),
        {"id": record_id, "ticket_id": ticket.id, "admin_id": scanner.id},
    )

    is_reversed = db_session.execute(
        text("SELECT is_reversed FROM check_in_records WHERE id = :id"), {"id": record_id}
    ).scalar_one()
    assert is_reversed is False
