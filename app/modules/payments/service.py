"""Simulated payments (BE-14). Everything is simulated: no real KYC, payouts or money.

Activation: an organizer completes a per-event checklist. Whichever step finishes it, in
any order, sets Event.paid_sales_active. Every step is idempotent.

Pay: a buyer pays a pending checkout order with a simulated card result. Success issues the
tickets; failure leaves the order pending so the buyer can retry until it expires.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.config import get_settings
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.enums import PayoutAccountStatus, VerificationStatus
from app.modules.identity.models import PayoutAccount, User
from app.modules.ops.enums import NotificationChannel, NotificationType
from app.modules.ops.models import Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order
from app.modules.orders.service import CLOSED_EVENT_STATUSES
from app.modules.payments import repository
from app.modules.payments.enums import PaymentPurpose, PaymentStatus
from app.modules.payments.models import Payment
from app.modules.payments.schemas import (
    ActivationChecklistOut,
    PaymentOut,
    PaymentResultOut,
    PayOrderIn,
)


def _checklist(session: Session, event: Event) -> ActivationChecklistOut:
    organizer = event.organizer
    return ActivationChecklistOut(
        has_paid_ticket_type=repository.has_paid_ticket_type(session, event.id),
        identity_verified=organizer.verification_status == VerificationStatus.VERIFIED,
        payout_account_ready=(
            repository.get_verified_payout_account(session, organizer.id) is not None
        ),
        activation_fee_paid=repository.get_succeeded_activation(session, event.id) is not None,
        terms_accepted=event.paid_terms_accepted_at is not None,
        active=event.paid_sales_active,
        activation_fee_kzt=get_settings().activation_fee_kzt,
    )


def _maybe_activate(session: Session, event: Event) -> ActivationChecklistOut:
    """Turn paid sales on once every checklist item is done. Never turns them off."""
    checklist = _checklist(session, event)
    done = (
        checklist.has_paid_ticket_type
        and checklist.identity_verified
        and checklist.payout_account_ready
        and checklist.activation_fee_paid
        and checklist.terms_accepted
    )
    if done and not event.paid_sales_active:
        event.paid_sales_active = True
        checklist.active = True
    return checklist


def _lock_open_event(session: Session, event_id: uuid.UUID) -> Event:
    """Lock the event row first, so two concurrent "last steps" cannot both read an
    incomplete checklist and leave the event inactive."""
    event = orders_repository.lock_event(session, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    if event.status in CLOSED_EVENT_STATUSES:
        raise BusinessRuleError(f"Paid sales cannot be activated on a {event.status} event")
    return event


def _finish_step(session: Session, event: Event) -> ActivationChecklistOut:
    checklist = _maybe_activate(session, event)
    session.commit()  # also releases the locks
    return checklist


def get_checklist(session: Session, event: Event) -> ActivationChecklistOut:
    return _checklist(session, event)


def simulate_verification(session: Session, event: Event) -> ActivationChecklistOut:
    """Simulated KYC and payout account. Applies to the organizer, so all of their events
    see it."""
    event = _lock_open_event(session, event.id)
    organizer = repository.lock_organizer(session, event.organizer_id)
    organizer.verification_status = VerificationStatus.VERIFIED
    if repository.get_verified_payout_account(session, organizer.id) is None:
        repository.add_payout_account(
            session,
            PayoutAccount(
                organizer_id=organizer.id,
                status=PayoutAccountStatus.VERIFIED,
                provider_ref=f"SIMULATED-{uuid.uuid4().hex[:12].upper()}",
            ),
        )
    return _finish_step(session, event)


def accept_terms(
    session: Session, event: Event, now: datetime | None = None
) -> ActivationChecklistOut:
    """Record acceptance of the paid-sales terms. Repeating it keeps the first timestamp."""
    event = _lock_open_event(session, event.id)
    if event.paid_terms_accepted_at is None:
        event.paid_terms_accepted_at = now or datetime.now(UTC)
    return _finish_step(session, event)


def pay_activation(session: Session, event: Event) -> ActivationChecklistOut:
    """Simulated activation fee. Paying again creates no second payment."""
    event = _lock_open_event(session, event.id)
    if repository.get_succeeded_activation(session, event.id) is None:
        repository.add_payment(
            session,
            Payment(
                purpose=PaymentPurpose.ACTIVATION,
                event_id=event.id,
                amount_kzt=get_settings().activation_fee_kzt,
                status=PaymentStatus.SUCCEEDED,
                is_simulated=True,
            ),
        )
    return _finish_step(session, event)


# --- paying a checkout order ---


def _result(session: Session, order: Order, payment: Payment) -> PaymentResultOut:
    # Reload the order with items and tickets from the DB: the response must show the
    # tickets just issued (or by a concurrent pay), never a stale in-session collection.
    order = orders_repository.get_order_with_details(session, order.id, reload=True)
    return PaymentResultOut(
        payment=PaymentOut.model_validate(payment, from_attributes=True),
        order=orders_service.order_out(order),
    )


def pay_order(
    session: Session,
    user: User,
    order_id: uuid.UUID,
    data: PayOrderIn,
    now: datetime | None = None,
) -> PaymentResultOut:
    """Pay a pending checkout order with a simulated card result.

    The order row is locked (FOR UPDATE) first, so two pays of the same order run one
    after the other: the second sees status = paid and returns the same result. The time is
    compared once, under that lock. Only the order is locked, so this cannot deadlock with
    checkout or registration (event -> ticket types).
    """
    now = now or datetime.now(UTC)
    order = orders_repository.lock_order(session, order_id)
    if order is None or order.buyer_user_id != user.id:
        raise NotFoundError("Order not found")

    if order.status == OrderStatus.PAID:
        # Already paid (a retry, or a concurrent pay that won): create nothing.
        return _result(session, order, repository.get_succeeded_order_payment(session, order.id))
    if order.status != OrderStatus.PENDING:
        raise BusinessRuleError("Order cannot be paid")
    if now >= order.created_at + orders_repository.checkout_hold():
        order.status = OrderStatus.CANCELLED
        session.commit()  # keep the cancellation, then report it
        raise BusinessRuleError("Checkout expired, start again")
    # Checked again at pay time: an admin may suspend paid sales (BE-22) after checkout.
    event = order.event
    if event.status != EventStatus.PUBLISHED or not event.paid_sales_active:
        raise BusinessRuleError("Paid ticket sales are not active for this event")

    # From the stored order, never from the client.
    amount = order.total_kzt - order.discount_kzt
    succeeded = data.simulate == "success"
    payment = Payment(
        purpose=PaymentPurpose.TICKET_ORDER,
        order_id=order.id,
        amount_kzt=amount,
        status=PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED,
        is_simulated=True,
    )
    repository.add_payment(session, payment)

    if succeeded:
        order.status = OrderStatus.PAID
        orders_service.issue_tickets(order)
        repository.add_notification(
            session,
            Notification(
                user_id=order.buyer_user_id,
                type=NotificationType.ORDER_CONFIRMED,
                channel=NotificationChannel.IN_APP,
                payload={"order_id": str(order.id)},
            ),
        )
    # A failed payment changes nothing else: the order stays pending and keeps its hold.
    session.commit()  # one commit: payment, status, tickets and notification together
    return _result(session, order, payment)
