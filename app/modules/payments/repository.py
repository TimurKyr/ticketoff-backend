import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.identity.enums import PayoutAccountStatus
from app.modules.identity.models import OrganizerProfile, PayoutAccount
from app.modules.ops.models import Notification
from app.modules.orders.enums import TicketKind
from app.modules.orders.models import Order, TicketType
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund


def has_paid_ticket_type(session: Session, event_id: uuid.UUID) -> bool:
    """Whether the event has a paid ticket type that buyers can see (hidden ones don't count)."""
    stmt = (
        select(TicketType.id)
        .where(
            TicketType.event_id == event_id,
            TicketType.kind == TicketKind.PAID,
            TicketType.is_hidden.is_(False),
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def lock_organizer(session: Session, organizer_id: uuid.UUID) -> OrganizerProfile | None:
    """SELECT ... FROM organizer_profiles WHERE id = :id FOR UPDATE, held until commit.

    Verification applies to the organizer, not one event, so two events of the same
    organizer are only serialized by this lock. Take it after the event lock.
    """
    stmt = (
        select(OrganizerProfile)
        .where(OrganizerProfile.id == organizer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalar(stmt)


def get_verified_payout_account(session: Session, organizer_id: uuid.UUID) -> PayoutAccount | None:
    stmt = (
        select(PayoutAccount)
        .where(
            PayoutAccount.organizer_id == organizer_id,
            PayoutAccount.status == PayoutAccountStatus.VERIFIED,
        )
        .limit(1)
    )
    return session.scalar(stmt)


def add_payout_account(session: Session, account: PayoutAccount) -> None:
    session.add(account)
    session.flush()


def get_succeeded_activation(session: Session, event_id: uuid.UUID) -> Payment | None:
    """The event's succeeded activation payment; uq_payments_activation_succeeded allows one."""
    stmt = select(Payment).where(
        Payment.event_id == event_id,
        Payment.purpose == PaymentPurpose.ACTIVATION,
        Payment.status == PaymentStatus.SUCCEEDED,
    )
    return session.scalar(stmt)


def get_succeeded_order_payment(session: Session, order_id: uuid.UUID) -> Payment | None:
    """The order's succeeded payment; uq_payments_order_succeeded allows one."""
    stmt = select(Payment).where(
        Payment.order_id == order_id,
        Payment.purpose == PaymentPurpose.TICKET_ORDER,
        Payment.status == PaymentStatus.SUCCEEDED,
    )
    return session.scalar(stmt)


def add_payment(session: Session, payment: Payment) -> None:
    session.add(payment)
    session.flush()


def add_notification(session: Session, notification: Notification) -> None:
    session.add(notification)
    session.flush()


def summary_totals(session: Session, event_id: uuid.UUID) -> tuple[int, int]:
    """(gross_kzt, refunds_kzt) for the event's ticket sales, in one round trip.

    gross: succeeded ticket_order payments of the event's orders (the activation fee is
    the organizer paying the platform, so it is not included). refunds: succeeded refunds
    of those payments. Two separate sums, not one join: a payment with several refunds
    would otherwise be counted once per refund in gross.
    """
    sold = (
        Payment.purpose == PaymentPurpose.TICKET_ORDER,
        Payment.status == PaymentStatus.SUCCEEDED,
        Order.event_id == event_id,
    )
    gross = (
        select(func.coalesce(func.sum(Payment.amount_kzt), 0))
        .join(Order, Order.id == Payment.order_id)
        .where(*sold)
        .scalar_subquery()
    )
    refunds = (
        select(func.coalesce(func.sum(Refund.amount_kzt), 0))
        .join(Payment, Payment.id == Refund.payment_id)
        .join(Order, Order.id == Payment.order_id)
        .where(*sold, Refund.status == RefundStatus.SUCCEEDED)
        .scalar_subquery()
    )
    row = session.execute(select(gross, refunds)).one()
    return int(row[0]), int(row[1])
