"""Paid sales activation checklist (BE-14), service level."""

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError
from app.core.config import get_settings
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.enums import PayoutAccountStatus, VerificationStatus
from app.modules.identity.models import OrganizerProfile, PayoutAccount
from app.modules.payments import service
from app.modules.payments.enums import PaymentPurpose, PaymentStatus
from app.modules.payments.models import Payment
from app.modules.payments.schemas import ActivationChecklistOut
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
)
from tests.markers import postgres_only

# Every step takes FOR UPDATE locks.
pytestmark = postgres_only

Step = Callable[[Session, Event], ActivationChecklistOut]
STEPS: dict[str, Step] = {
    "simulate_verification": service.simulate_verification,
    "accept_terms": service.accept_terms,
    "pay_activation": service.pay_activation,
}


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    return create_event(db_session, organizer)


def _payments(session: Session, event: Event) -> list[Payment]:
    return list(session.scalars(select(Payment).where(Payment.event_id == event.id)))


def _payout_accounts(session: Session, organizer: OrganizerProfile) -> int:
    return session.scalar(
        select(func.count(PayoutAccount.id)).where(PayoutAccount.organizer_id == organizer.id)
    )


# --- checklist ---


def test_fresh_event_has_nothing_done(db_session: Session, event: Event) -> None:
    checklist = service.get_checklist(db_session, event)

    assert checklist == ActivationChecklistOut(
        has_paid_ticket_type=False,
        identity_verified=False,
        payout_account_ready=False,
        activation_fee_paid=False,
        terms_accepted=False,
        active=False,
        activation_fee_kzt=get_settings().activation_fee_kzt,
    )
    assert checklist.is_simulated is True


def test_only_visible_paid_types_count(db_session: Session, event: Event) -> None:
    create_free_ticket_type(db_session, event)
    create_ticket_type(db_session, event, name="Secret", is_hidden=True)
    assert service.get_checklist(db_session, event).has_paid_ticket_type is False

    create_ticket_type(db_session, event, name="Standard")
    assert service.get_checklist(db_session, event).has_paid_ticket_type is True


@pytest.mark.parametrize(
    ("step", "flags"),
    [
        ("simulate_verification", {"identity_verified", "payout_account_ready"}),
        ("accept_terms", {"terms_accepted"}),
        ("pay_activation", {"activation_fee_paid"}),
    ],
)
def test_each_step_sets_only_its_flags(
    db_session: Session, event: Event, step: str, flags: set[str]
) -> None:
    checklist = STEPS[step](db_session, event).model_dump()

    done = {name for name, value in checklist.items() if value is True}
    assert done == flags | {"is_simulated"}


# --- activation ---


@pytest.mark.parametrize("order", list(itertools.permutations(STEPS)))
def test_activates_only_after_the_last_step_in_any_order(
    db_session: Session, event: Event, order: tuple[str, ...]
) -> None:
    create_ticket_type(db_session, event)
    *first, last = order

    for name in first:
        assert STEPS[name](db_session, event).active is False
        assert event.paid_sales_active is False

    assert STEPS[last](db_session, event).active is True
    db_session.expire_all()
    assert db_session.get(Event, event.id).paid_sales_active is True


def test_paid_type_added_last_activates_on_the_next_step(db_session: Session, event: Event) -> None:
    """Adding a ticket type does not call the checklist. Repeating any step (they are all
    idempotent) does."""
    for step in STEPS.values():
        step(db_session, event)
    assert service.get_checklist(db_session, event).active is False

    create_ticket_type(db_session, event)

    assert service.get_checklist(db_session, event).active is False
    assert service.accept_terms(db_session, event).active is True


def test_no_visible_paid_type_never_activates(db_session: Session, event: Event) -> None:
    create_free_ticket_type(db_session, event)
    create_ticket_type(db_session, event, name="Secret", is_hidden=True)

    for step in STEPS.values():
        checklist = step(db_session, event)

    assert checklist.has_paid_ticket_type is False
    assert checklist.active is False
    assert event.paid_sales_active is False


def test_draft_event_can_be_activated(db_session: Session, organizer: OrganizerProfile) -> None:
    draft = create_event(db_session, organizer, status=EventStatus.DRAFT)
    create_ticket_type(db_session, draft)

    for step in STEPS.values():
        checklist = step(db_session, draft)

    assert checklist.active is True


@pytest.mark.parametrize("status", [EventStatus.CANCELLED, EventStatus.COMPLETED])
@pytest.mark.parametrize("step", list(STEPS))
def test_closed_event_is_rejected(
    db_session: Session, organizer: OrganizerProfile, status: EventStatus, step: str
) -> None:
    closed = create_event(db_session, organizer, status=status)

    with pytest.raises(BusinessRuleError, match=f"on a {status} event"):
        STEPS[step](db_session, closed)

    # Rejected before any change.
    assert _payments(db_session, closed) == []
    assert db_session.get(Event, closed.id).paid_terms_accepted_at is None


# --- idempotency ---


def test_pay_twice_creates_one_simulated_payment(db_session: Session, event: Event) -> None:
    service.pay_activation(db_session, event)
    checklist = service.pay_activation(db_session, event)

    [payment] = _payments(db_session, event)
    assert payment.purpose == PaymentPurpose.ACTIVATION
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.is_simulated is True
    assert payment.order_id is None
    assert payment.amount_kzt == get_settings().activation_fee_kzt
    assert checklist.activation_fee_paid is True


def test_accept_terms_twice_keeps_first_timestamp(db_session: Session, event: Event) -> None:
    first = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    service.accept_terms(db_session, event, now=first)
    service.accept_terms(db_session, event, now=first + timedelta(days=1))

    db_session.expire_all()
    assert db_session.get(Event, event.id).paid_terms_accepted_at == first


def test_verification_twice_creates_one_payout_account(
    db_session: Session, event: Event, organizer: OrganizerProfile
) -> None:
    service.simulate_verification(db_session, event)
    service.simulate_verification(db_session, event)

    [account] = db_session.scalars(
        select(PayoutAccount).where(PayoutAccount.organizer_id == organizer.id)
    ).all()
    assert account.status == PayoutAccountStatus.VERIFIED
    assert account.provider_ref.startswith("SIMULATED-")
    assert organizer.verification_status == VerificationStatus.VERIFIED


def test_verification_reuses_an_existing_verified_account(
    db_session: Session, event: Event, organizer: OrganizerProfile
) -> None:
    db_session.add_all(
        [
            PayoutAccount(organizer_id=organizer.id, status=PayoutAccountStatus.REJECTED),
            PayoutAccount(organizer_id=organizer.id, status=PayoutAccountStatus.VERIFIED),
        ]
    )
    db_session.flush()

    checklist = service.simulate_verification(db_session, event)

    assert checklist.payout_account_ready is True
    assert _payout_accounts(db_session, organizer) == 2


def test_verification_applies_to_other_events_of_the_organizer(
    db_session: Session, event: Event, organizer: OrganizerProfile
) -> None:
    other = create_event(db_session, organizer)
    stranger_event = create_event(db_session, create_organizer(db_session))

    service.simulate_verification(db_session, event)

    other_checklist = service.get_checklist(db_session, other)
    assert other_checklist.identity_verified is True
    assert other_checklist.payout_account_ready is True
    assert service.get_checklist(db_session, stranger_event).identity_verified is False
    # Fee and terms stay per event.
    assert other_checklist.activation_fee_paid is False
    assert other_checklist.terms_accepted is False
