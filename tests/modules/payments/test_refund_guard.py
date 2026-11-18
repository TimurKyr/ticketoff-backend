"""uq_refunds_payment_succeeded (BE-19): at most one succeeded refund per payment."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders.enums import OrderStatus
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from tests.factories import create_event, create_order_item, create_organizer, create_ticket_type
from tests.markers import postgres_only

# A partial unique index (WHERE status = 'succeeded').
pytestmark = postgres_only


@pytest.fixture
def payment(db_session: Session) -> Payment:
    ticket_type = create_ticket_type(
        db_session, create_event(db_session, create_organizer(db_session))
    )
    item = create_order_item(db_session, ticket_type, 2, OrderStatus.PAID)
    payment = Payment(
        purpose=PaymentPurpose.TICKET_ORDER,
        order_id=item.order_id,
        amount_kzt=10000,
        status=PaymentStatus.SUCCEEDED,
    )
    db_session.add(payment)
    db_session.flush()
    return payment


def add_refund(session: Session, payment: Payment, status: RefundStatus) -> None:
    session.add(Refund(payment_id=payment.id, amount_kzt=10000, status=status))
    session.flush()


def test_second_succeeded_refund_is_refused(db_session: Session, payment: Payment) -> None:
    add_refund(db_session, payment, RefundStatus.SUCCEEDED)

    with pytest.raises(IntegrityError, match="uq_refunds_payment_succeeded"):
        add_refund(db_session, payment, RefundStatus.SUCCEEDED)


def test_failed_refunds_do_not_block_a_succeeded_one(db_session: Session, payment: Payment) -> None:
    add_refund(db_session, payment, RefundStatus.FAILED)
    add_refund(db_session, payment, RefundStatus.FAILED)

    add_refund(db_session, payment, RefundStatus.SUCCEEDED)  # must not raise
