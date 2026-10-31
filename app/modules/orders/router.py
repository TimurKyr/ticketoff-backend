import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, status

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.orders import service
from app.modules.orders.schemas import OrderOut, RegistrationCreate, TicketOut

registrations_router = APIRouter(prefix="/events", tags=["registrations"])
orders_router = APIRouter(prefix="/orders", tags=["orders"])
tickets_router = APIRouter(prefix="/tickets", tags=["tickets"])


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@registrations_router.post(
    "/{event_id}/registrations",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register for free tickets",
)
def register_free(
    event_id: uuid.UUID, payload: RegistrationCreate, user: CurrentUser, session: SessionDep
) -> OrderOut:
    with _http_errors():
        return service.register_free(session, user, event_id, payload)


# Declared before /{order_id} so "me" is not parsed as an order id.
@orders_router.get("/me", response_model=list[OrderOut], summary="My orders")
def list_my_orders(user: CurrentUser, session: SessionDep) -> list[OrderOut]:
    return service.list_my_orders(session, user)


@orders_router.get("/{order_id}", response_model=OrderOut, summary="One of my orders")
def get_my_order(order_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> OrderOut:
    with _http_errors():
        return service.get_my_order(session, user, order_id)


@tickets_router.get(
    "/{ticket_id}", response_model=TicketOut, summary="One of my tickets, with its QR token"
)
def get_my_ticket(ticket_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> TicketOut:
    with _http_errors():
        return service.get_my_ticket(session, user, ticket_id)
