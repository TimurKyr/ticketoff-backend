"""Paid sales activation (BE-14). Everything is simulated: no real KYC, payouts or money.

An organizer completes a per-event checklist. Whichever step finishes it, in any order,
sets Event.paid_sales_active. Every step is idempotent, so repeating one is harmless.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.config import get_settings
from app.modules.events.models import Event
from app.modules.identity.enums import PayoutAccountStatus, VerificationStatus
from app.modules.identity.models import PayoutAccount
from app.modules.orders import repository as orders_repository
from app.modules.orders.service import CLOSED_EVENT_STATUSES
from app.modules.payments import repository
from app.modules.payments.enums import PaymentPurpose, PaymentStatus
from app.modules.payments.models import Payment
from app.modules.payments.schemas import ActivationChecklistOut


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
