from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.identity.dependencies import SessionDep
from app.modules.payments import service
from app.modules.payments.schemas import ActivationChecklistOut

# Owner only: someone else's event answers 404 (require_event_owner).
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]

activation_router = APIRouter(prefix="/events/{event_id}/activation", tags=["activation"])


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
