import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.payments import service
from app.modules.payments.schemas import (
    ActivationChecklistOut,
    PaymentResultOut,
    PaymentSummaryOut,
    PayOrderIn,
    RefundIn,
    RefundResultOut,
)

# Owner only: someone else's event answers 404 (require_event_owner).
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]

activation_router = APIRouter(prefix="/events/{event_id}/activation", tags=["activation"])
payments_router = APIRouter(tags=["payments"])


@activation_router.get(
    "", response_model=ActivationChecklistOut, summary="Paid sales checklist (simulated)"
)
def get_checklist(event: OwnedEventDep, session: SessionDep) -> ActivationChecklistOut:
    return service.get_checklist(session, event)


@activation_router.post(
    "/simulate-verification",
    response_model=ActivationChecklistOut,
    summary="Simulate identity verification and a payout account",
)
def simulate_verification(event: OwnedEventDep, session: SessionDep) -> ActivationChecklistOut:
    return service.simulate_verification(session, event)


@activation_router.post(
    "/accept-terms", response_model=ActivationChecklistOut, summary="Accept paid sales terms"
)
def accept_terms(event: OwnedEventDep, session: SessionDep) -> ActivationChecklistOut:
    return service.accept_terms(session, event)


@activation_router.post(
    "/pay", response_model=ActivationChecklistOut, summary="Pay the activation fee (simulated)"
)
def pay_activation(
    event: OwnedEventDep, user: CurrentUser, session: SessionDep
) -> ActivationChecklistOut:
    return service.pay_activation(session, event, user)


@payments_router.post(
    "/orders/{order_id}/pay",
    response_model=PaymentResultOut,
    summary="Pay a pending order (simulated card: success or fail)",
)
def pay_order(
    order_id: uuid.UUID, payload: PayOrderIn, user: CurrentUser, session: SessionDep
) -> PaymentResultOut:
    """200 for both results: on "fail" the payment is failed and the order stays pending."""
    return service.pay_order(session, user, order_id, payload)


@payments_router.post(
    "/orders/{order_id}/refund",
    response_model=RefundResultOut,
    summary="Fully refund a paid order of my event (simulated)",
)
def refund_order(
    order_id: uuid.UUID, payload: RefundIn, user: CurrentUser, session: SessionDep
) -> RefundResultOut:
    """Event organizer only; anyone else, the buyer included, gets 404."""
    return service.refund_order(session, user, order_id, payload)


@payments_router.get(
    "/events/{event_id}/payments/summary",
    response_model=PaymentSummaryOut,
    summary="Simulated revenue summary for the organizer",
)
def get_summary(event: OwnedEventDep, session: SessionDep) -> PaymentSummaryOut:
    return service.get_summary(session, event)
