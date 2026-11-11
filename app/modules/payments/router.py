import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.payments import service
from app.modules.payments.schemas import (
    ActivationChecklistOut,
    PaymentResultOut,
    PaymentSummaryOut,
    PayOrderIn,
)

# Owner only: someone else's event answers 404 (require_event_owner).
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]

activation_router = APIRouter(prefix="/events/{event_id}/activation", tags=["activation"])
payments_router = APIRouter(tags=["payments"])


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
    with _http_errors():
        return service.simulate_verification(session, event)


@activation_router.post(
    "/accept-terms", response_model=ActivationChecklistOut, summary="Accept paid sales terms"
)
def accept_terms(event: OwnedEventDep, session: SessionDep) -> ActivationChecklistOut:
    with _http_errors():
        return service.accept_terms(session, event)


@activation_router.post(
    "/pay", response_model=ActivationChecklistOut, summary="Pay the activation fee (simulated)"
)
def pay_activation(event: OwnedEventDep, session: SessionDep) -> ActivationChecklistOut:
    with _http_errors():
        return service.pay_activation(session, event)


@payments_router.post(
    "/orders/{order_id}/pay",
    response_model=PaymentResultOut,
    summary="Pay a pending order (simulated card: success or fail)",
)
def pay_order(
    order_id: uuid.UUID, payload: PayOrderIn, user: CurrentUser, session: SessionDep
) -> PaymentResultOut:
    """200 for both results: on "fail" the payment is failed and the order stays pending."""
    with _http_errors():
        return service.pay_order(session, user, order_id, payload)


@payments_router.get(
    "/events/{event_id}/payments/summary",
    response_model=PaymentSummaryOut,
    summary="Simulated revenue summary for the organizer",
)
def get_summary(event: OwnedEventDep, session: SessionDep) -> PaymentSummaryOut:
    return service.get_summary(session, event)
