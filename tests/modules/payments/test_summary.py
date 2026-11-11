"""Organizer payments summary (BE-14): service math and the owner-only endpoint."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile
from app.modules.identity.security import create_access_token
from app.modules.orders.enums import OrderStatus
from app.modules.payments import repository, service
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from tests.factories import create_event, create_order_item, create_organizer, create_ticket_type


@pytest.fixture(autouse=True)
def three_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The math below assumes the default 3%, whatever the local .env says."""
    monkeypatch.setattr(get_settings(), "processing_fee_percent", 3)


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    return create_event(db_session, organizer)


def _order_payment(
    session: Session,
    event: Event,
    amount: int,
    status: PaymentStatus = PaymentStatus.SUCCEEDED,
) -> Payment:
    """A ticket_order payment on a new order of the event."""
    item = create_order_item(session, create_ticket_type(session, event), 1, OrderStatus.PAID)
    payment = Payment(
        purpose=PaymentPurpose.TICKET_ORDER,
        order_id=item.order_id,
        amount_kzt=amount,
        status=status,
    )
    session.add(payment)
    session.flush()
    return payment


def _refund(session: Session, payment: Payment, amount: int, status: RefundStatus) -> None:
    session.add(Refund(payment_id=payment.id, amount_kzt=amount, status=status))
    session.flush()


def _totals(session: Session, event: Event) -> tuple[int, int, int, int]:
    s = service.get_summary(session, event)
    return s.gross_kzt, s.fees_kzt, s.refunds_kzt, s.estimated_payout_kzt


def test_empty_event_is_all_zeros(db_session: Session, event: Event) -> None:
    summary = service.get_summary(db_session, event)

    assert summary.model_dump() == {
        "gross_kzt": 0,
        "fees_kzt": 0,
        "refunds_kzt": 0,
        "estimated_payout_kzt": 0,
        "fee_percent": 3,
        "currency": "KZT",
        "is_simulated": True,
    }


def test_sums_succeeded_ticket_payments(db_session: Session, event: Event) -> None:
    _order_payment(db_session, event, 100_000)
    _order_payment(db_session, event, 50_000)

    assert _totals(db_session, event) == (150_000, 4_500, 0, 145_500)


def test_failed_and_pending_payments_and_activation_fee_are_excluded(
    db_session: Session, event: Event
) -> None:
    _order_payment(db_session, event, 10_000)
    _order_payment(db_session, event, 20_000, PaymentStatus.FAILED)
    _order_payment(db_session, event, 40_000, PaymentStatus.PENDING)
    db_session.add(
        Payment(
            purpose=PaymentPurpose.ACTIVATION,
            event_id=event.id,
            amount_kzt=5_000,
            status=PaymentStatus.SUCCEEDED,
        )
    )
    db_session.flush()

    assert _totals(db_session, event)[0] == 10_000


def test_other_events_payments_are_excluded(
    db_session: Session, event: Event, organizer: OrganizerProfile
) -> None:
    _order_payment(db_session, event, 10_000)
    _order_payment(db_session, create_event(db_session, organizer), 99_000)

    assert _totals(db_session, event)[0] == 10_000


@pytest.mark.parametrize(
    ("gross", "fee"),
    [
        (10_050, 302),  # 301.5 -> 302 (half up)
        (10_016, 300),  # 300.48 -> 300
        (10_017, 301),  # 300.51 -> 301
        (50, 2),  # 1.5 -> 2
        (1, 0),  # 0.03 -> 0
    ],
)
def test_fee_rounds_half_up(db_session: Session, event: Event, gross: int, fee: int) -> None:
    _order_payment(db_session, event, gross)

    assert _totals(db_session, event) == (gross, fee, 0, gross - fee)


def test_succeeded_refunds_are_subtracted_pending_and_failed_are_not(
    db_session: Session, event: Event
) -> None:
    payment = _order_payment(db_session, event, 100_000)
    _refund(db_session, payment, 15_000, RefundStatus.SUCCEEDED)
    _refund(db_session, payment, 5_000, RefundStatus.SUCCEEDED)
    _refund(db_session, payment, 30_000, RefundStatus.PENDING)
    _refund(db_session, payment, 40_000, RefundStatus.FAILED)

    # Several refunds on one payment do not multiply the gross.
    assert _totals(db_session, event) == (100_000, 3_000, 20_000, 77_000)


def test_refunds_of_failed_payments_and_other_events_are_excluded(
    db_session: Session, event: Event, organizer: OrganizerProfile
) -> None:
    failed = _order_payment(db_session, event, 10_000, PaymentStatus.FAILED)
    _refund(db_session, failed, 1_000, RefundStatus.SUCCEEDED)
    elsewhere = _order_payment(db_session, create_event(db_session, organizer), 10_000)
    _refund(db_session, elsewhere, 2_000, RefundStatus.SUCCEEDED)

    assert repository.summary_totals(db_session, event.id) == (0, 0)


# --- endpoint ---


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def _headers(organizer: OrganizerProfile) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(organizer.user_id)}"}


def test_owner_gets_summary(
    api: TestClient, db_session: Session, event: Event, organizer: OrganizerProfile
) -> None:
    _order_payment(db_session, event, 150_000)

    response = api.get(f"/events/{event.id}/payments/summary", headers=_headers(organizer))

    assert response.status_code == 200
    assert response.json() == {
        "gross_kzt": 150_000,
        "fees_kzt": 4_500,
        "refunds_kzt": 0,
        "estimated_payout_kzt": 145_500,
        "fee_percent": 3,
        "currency": "KZT",
        "is_simulated": True,
    }


def test_other_organizer_gets_404(api: TestClient, db_session: Session, event: Event) -> None:
    stranger = create_organizer(db_session)

    response = api.get(f"/events/{event.id}/payments/summary", headers=_headers(stranger))

    assert response.status_code == 404


def test_requires_login(api: TestClient, event: Event) -> None:
    assert api.get(f"/events/{event.id}/payments/summary").status_code == 401
