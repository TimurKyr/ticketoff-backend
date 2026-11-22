"""Simulated payments (BE-14). Everything is simulated: no real KYC, payouts or money.

Activation: an organizer completes a per-event checklist. Whichever step finishes it, in
any order, sets Event.paid_sales_active. Every step is idempotent.

Pay: a buyer pays a pending checkout order with a simulated card result. Success issues the
tickets; failure leaves the order pending so the buyer can retry until it expires.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.config import get_settings
from app.modules.audit import service as audit
from app.modules.events import service as events_service
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.enums import PayoutAccountStatus, VerificationStatus
from app.modules.identity.models import PayoutAccount, User
from app.modules.notifications import flows as notification_flows
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import OrderStatus, TicketStatus
from app.modules.orders.models import Order, Ticket
from app.modules.orders.service import CLOSED_EVENT_STATUSES
from app.modules.payments import repository
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from app.modules.payments.schemas import (
    ActivationChecklistOut,
    PaymentOut,
    PaymentResultOut,
    PaymentSummaryOut,
    PayOrderIn,
    RefundIn,
    RefundOut,
    RefundResultOut,
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


def _payment_audit_details(payment: Payment) -> dict[str, object]:
    """details for a payment's audit row. Audited only when a payment row is created, so an
    idempotent repeat writes nothing."""
    details: dict[str, object] = {
        "amount_kzt": payment.amount_kzt,
        "is_simulated": payment.is_simulated,
    }
    if payment.order_id is not None:
        details["order_id"] = str(payment.order_id)
    return details


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


def pay_activation(session: Session, event: Event, user: User) -> ActivationChecklistOut:
    """Simulated activation fee. Paying again creates no second payment and no audit row."""
    event = _lock_open_event(session, event.id)
    if repository.get_succeeded_activation(session, event.id) is None:
        payment = Payment(
            purpose=PaymentPurpose.ACTIVATION,
            event_id=event.id,
            amount_kzt=get_settings().activation_fee_kzt,
            status=PaymentStatus.SUCCEEDED,
            is_simulated=True,
        )
        repository.add_payment(session, payment)
        audit.log(
            session,
            actor_user_id=user.id,
            action_type="payment.activation_succeeded",
            entity_type="payment",
            entity_id=payment.id,
            event_id=event.id,
            details=_payment_audit_details(payment),
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

    Locks (FOR UPDATE) the order's event first, then the order: the same order as checkout
    and registration (event first, then the rest), so they never wait on each other in a
    cycle. The event lock serializes pay with checkouts of the same event, and the clock
    is read only after both locks. So a checkout that already counted this order as expired
    and sold its tickets has committed before pay looks at the time, and pay sees the order
    expired too; and a pay that goes first makes the order paid before the checkout counts.
    The order lock serializes two pays of one order: the second sees status = paid and
    returns the same result.
    """
    # Unlocked read, only to find the event to lock and to answer 404 early.
    order = orders_repository.get_order(session, order_id)
    if order is None or order.buyer_user_id != user.id:
        raise NotFoundError("Order not found")
    event = orders_repository.lock_event(session, order.event_id)
    order = orders_repository.lock_order(session, order_id)  # re-read under both locks
    now = now or datetime.now(UTC)

    if order.status == OrderStatus.PAID:
        # Already paid (a retry, or a concurrent pay that won): create nothing.
        return _result(session, order, repository.get_succeeded_order_payment(session, order.id))
    if order.status != OrderStatus.PENDING:
        raise BusinessRuleError("Order cannot be paid")
    # created_at is the DB's now() (start of the checkout transaction); `now` is the app clock.
    if now >= order.created_at + orders_repository.checkout_hold():
        order.status = OrderStatus.CANCELLED
        session.commit()  # keep the cancellation, then report it
        raise BusinessRuleError("Checkout expired, start again")
    # Same clock and locked event row as the expiry check above.
    if now >= event.start_at:
        raise BusinessRuleError("Event has already started")
    # Checked again at pay time, on the locked row: an admin may suspend sales (BE-22)
    # after checkout. The order stays pending and expires with its hold.
    if event.sales_suspended:
        raise BusinessRuleError(orders_service.SALES_SUSPENDED_MESSAGE)
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
    audit.log(
        session,
        actor_user_id=user.id,
        action_type="payment.order_succeeded" if succeeded else "payment.order_failed",
        entity_type="payment",
        entity_id=payment.id,
        # An order payment must not carry event_id (ck_payments_ticket_order_links_order_only);
        # the audit row gets it from the order.
        event_id=order.event_id,
        details=_payment_audit_details(payment),
    )

    if succeeded:
        order.status = OrderStatus.PAID
        orders_service.issue_tickets(order)
        notification_flows.order_confirmed(session, order)
    else:
        # A failed payment changes nothing else: the order stays pending and keeps its hold.
        notification_flows.payment_failed(session, order, payment)
    # One commit: payment, audit row, status, tickets and notification. Emails after it.
    session.commit()
    return _result(session, order, payment)


# --- refunding a paid order (BE-19) ---


def refund_locked_order(
    session: Session, order: Order, tickets: Sequence[Ticket], reason: str | None
) -> Refund:
    """Fully refund a paid order: a succeeded Refund of its payment, and the order and every
    given ticket (used ones too) become refunded. Nothing else.

    The caller has locked the event, the order and these tickets, and does the checks, the
    notification, the audit row and the commit (refund_order, or the event-cancel cascade).
    """
    payment = repository.get_succeeded_order_payment(session, order.id)
    if payment is None:  # paid is only ever set together with a succeeded payment
        raise RuntimeError(f"Paid order {order.id} has no succeeded payment")
    refund = Refund(
        payment_id=payment.id,
        amount_kzt=payment.amount_kzt,  # full refunds only
        status=RefundStatus.SUCCEEDED,  # the model default is pending
        reason=reason,
    )
    repository.add_refund(session, refund)
    order.status = OrderStatus.REFUNDED
    for ticket in tickets:
        ticket.status = TicketStatus.REFUNDED
    return refund


def refund_order(
    session: Session,
    user: User,
    order_id: uuid.UUID,
    data: RefundIn,
) -> RefundResultOut:
    """Fully refund a paid order (simulated). Only the event's organizer may do it.

    Anyone else, the buyer included, gets 404: the order's existence is not revealed.
    Allowed after the event started (the organizer's decision). Every ticket becomes
    refunded, used ones too, and the order becomes refunded, which releases its inventory.

    Locks: event, then order, then the order's tickets (repository lock order), so two
    refunds of one order run one after the other (the second gets 409) and a scan at the
    door either waits and answers `refunded` or wins first. uq_refunds_payment_succeeded
    refuses a second succeeded refund even if the locks were bypassed.
    """
    # Unlocked read: only to find the event to lock and to answer 404 early.
    order = orders_repository.get_order(session, order_id)
    if order is None or order.event.organizer.user_id != user.id:
        raise NotFoundError("Order not found")
    orders_repository.lock_event(session, order.event_id)
    order = orders_repository.lock_order(session, order_id)
    tickets = orders_repository.lock_order_tickets(session, order.id)
    # No time rules for refunds (allowed after the event started), so no clock is read.

    reason = orders_service.refund_block_reason(order)  # shared with the organizer's list
    if reason is not None:
        raise BusinessRuleError(reason)

    refund = refund_locked_order(session, order, tickets, data.reason)
    payment = refund.payment
    notification_flows.refund_processed(session, order, refund)
    audit.log(
        session,
        actor_user_id=user.id,
        action_type="order.refunded",
        entity_type="order",
        entity_id=order.id,
        event_id=order.event_id,
        details={
            "refund_id": str(refund.id),
            "payment_id": str(payment.id),
            "amount_kzt": refund.amount_kzt,
            "reason": refund.reason,
            "is_simulated": payment.is_simulated,
        },
    )
    session.commit()  # refund, statuses, notification and audit row together

    order = orders_repository.get_order_with_details(session, order.id, reload=True)
    return RefundResultOut(
        refund=RefundOut(
            id=refund.id,
            amount_kzt=refund.amount_kzt,
            status=refund.status,
            reason=refund.reason,
            is_simulated=payment.is_simulated,
        ),
        order=orders_service.order_out(order),
    )


# --- cancelling an event with its orders (BE-19) ---

EVENT_CANCEL_REFUND_REASON = "Event cancelled"


def cancel_event_with_orders(session: Session, event: Event, user: User) -> Event:
    """Cancel the organizer's event and, in the same transaction, every order on it.

    free_confirmed -> cancelled (tickets too, checked-in ones included: the event is off);
    paid -> full simulated refund, order and tickets refunded; pending (fresh or expired)
    -> cancelled; already cancelled or refunded -> untouched. One "event.cancelled" audit
    row with the counts, no per-order rows. One event_cancelled notification per buyer
    whose orders changed here, with what was refunded to them (BE-18); the emails go after
    the commit. Ownership is checked by the route.

    Locks, in the repository lock order: the event (mark_cancelled), then all of its orders,
    then all of its tickets. Checkout, registration, pay, cancel and refund lock the event
    first too, so they wait and then see the cancelled event; a scan at the door locks the
    ticket row, so it waits and then sees the new ticket status.
    """
    events_service.mark_cancelled(session, event)  # 409 unless draft/published
    orders = orders_repository.lock_event_orders(session, event.id)
    tickets_by_order: dict[uuid.UUID, list[Ticket]] = defaultdict(list)
    for ticket in orders_repository.lock_event_tickets(session, event.id):
        tickets_by_order[ticket.order_item.order_id].append(ticket)

    cancelled_orders = refunded_orders = refunded_kzt = pending_cancelled = ticket_count = 0
    changed: list[Order] = []
    refunded_by_order: dict[uuid.UUID, int] = {}
    for order in orders:
        tickets = tickets_by_order[order.id]
        if order.status == OrderStatus.FREE_CONFIRMED:
            orders_service.cancel_locked_order(order, tickets)
            cancelled_orders += 1
        elif order.status == OrderStatus.PAID:
            refund = refund_locked_order(session, order, tickets, EVENT_CANCEL_REFUND_REASON)
            refunded_orders += 1
            refunded_kzt += refund.amount_kzt
            refunded_by_order[order.id] = refund.amount_kzt
        elif order.status == OrderStatus.PENDING:
            orders_service.cancel_locked_order(order, tickets)  # no tickets yet
            pending_cancelled += 1
        else:  # cancelled or refunded already
            continue
        changed.append(order)
        ticket_count += len(tickets)

    notification_flows.event_cancelled(session, event, changed, refunded_by_order)

    audit.log(
        session,
        actor_user_id=user.id,
        action_type="event.cancelled",
        entity_type="event",
        entity_id=event.id,
        event_id=event.id,
        details={
            "cancelled_orders": cancelled_orders,
            "refunded_orders": refunded_orders,
            "refunded_kzt": refunded_kzt,
            "pending_cancelled": pending_cancelled,
            "ticket_count": ticket_count,
        },
    )
    session.commit()  # event, orders, tickets, refunds, audit row and notifications together
    return event


# --- organizer summary ---


def _percent_half_up(amount: int, percent: int) -> int:
    """amount * percent / 100 rounded half up, in integer KZT (no floats)."""
    return (amount * percent + 50) // 100


def fee_for(gross: int, percent: int) -> int:
    """The simulated processing fee on `gross`, one rule for the summary and the analytics."""
    return _percent_half_up(gross, percent)


def get_summary(session: Session, event: Event) -> PaymentSummaryOut:
    gross, refunds = repository.summary_totals(session, event.id)
    percent = get_settings().processing_fee_percent
    fees = fee_for(gross, percent)
    return PaymentSummaryOut(
        gross_kzt=gross,
        fees_kzt=fees,
        refunds_kzt=refunds,
        estimated_payout_kzt=gross - fees - refunds,
        fee_percent=percent,
    )
