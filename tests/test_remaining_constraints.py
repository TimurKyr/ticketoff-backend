"""Database rules added in BE-10: they must hold even if service code has a bug."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.events.models import Event, Row, Seat, VenueSection
from app.modules.orders.models import Order
from app.modules.payments.enums import PaymentPurpose
from app.modules.payments.models import Payment
from app.modules.promotions.enums import DiscountType
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
from app.modules.seating.enums import SeatHoldStatus
from app.modules.seating.models import SeatHold
from tests.factories import create_event, create_organizer, create_user

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


def add_hold(session: Session, seat: Seat, event: Event, status: SeatHoldStatus) -> None:
    session.add(SeatHold(seat_id=seat.id, event_id=event.id, held_until=HELD_UNTIL, status=status))
    session.flush()


def test_seat_cannot_have_two_active_holds(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    seat = create_seat(db_session, event)
    add_hold(db_session, seat, event, SeatHoldStatus.HELD)

    with pytest.raises(IntegrityError, match="uq_seat_holds_active_seat"):
        add_hold(db_session, seat, event, SeatHoldStatus.HELD)


def test_released_hold_does_not_block_new_hold(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    seat = create_seat(db_session, event)
    add_hold(db_session, seat, event, SeatHoldStatus.RELEASED)

    add_hold(db_session, seat, event, SeatHoldStatus.HELD)  # must not raise


@pytest.mark.parametrize(
    ("purpose", "with_order", "with_event", "amount", "constraint"),
    [
        (PaymentPurpose.TICKET_ORDER, False, True, 1000, "ticket_order_has_order"),
        (PaymentPurpose.ACTIVATION, False, False, 1000, "activation_has_event"),
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
