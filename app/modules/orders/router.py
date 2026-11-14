import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.orders import service
from app.modules.orders.schemas import (
    OrderOut,
    PublicTicketTypeOut,
    RegistrationCreate,
    TicketOut,
    TicketTypeCreate,
    TicketTypeOut,
    TicketTypeUpdate,
)

ticket_types_router = APIRouter(prefix="/events", tags=["ticket types"])
registrations_router = APIRouter(prefix="/events", tags=["registrations"])
orders_router = APIRouter(prefix="/orders", tags=["orders"])
tickets_router = APIRouter(prefix="/tickets", tags=["tickets"])

# The path's event, if the logged-in organizer owns it; otherwise 404 (403 if not an organizer).
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]


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


# --- ticket types ---
# No DELETE: a type is hidden with PATCH {"is_hidden": true}, never removed once sold.


@ticket_types_router.post(
    "/{event_id}/ticket-types",
    response_model=TicketTypeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a ticket type to my event",
)
def create_ticket_type(
    payload: TicketTypeCreate, event: OwnedEventDep, session: SessionDep
) -> TicketTypeOut:
    with _http_errors():
        return service.create_ticket_type(session, event.organizer_id, event.id, payload)


@ticket_types_router.get(
    "/{event_id}/ticket-types",
    response_model=list[TicketTypeOut],
    summary="Ticket types of my event, hidden ones included",
)
def list_ticket_types(event: OwnedEventDep, session: SessionDep) -> list[TicketTypeOut]:
    with _http_errors():
        return service.list_ticket_types(session, event.organizer_id, event.id)


# Declared before any GET /{ticket_type_id} route, so "public" is never parsed as an id.
@ticket_types_router.get(
    "/{event_id}/ticket-types/public",
    response_model=list[PublicTicketTypeOut],
    summary="Ticket types on sale now (no login)",
)
def list_public_ticket_types(event_id: uuid.UUID, session: SessionDep) -> list[PublicTicketTypeOut]:
    with _http_errors():
        return service.list_public_ticket_types(session, event_id)


@ticket_types_router.patch(
    "/{event_id}/ticket-types/{ticket_type_id}",
    response_model=TicketTypeOut,
    summary="Edit or hide a ticket type of my event",
)
def update_ticket_type(
    ticket_type_id: uuid.UUID,
    payload: TicketTypeUpdate,
    event: OwnedEventDep,
    session: SessionDep,
) -> TicketTypeOut:
    with _http_errors():
        return service.update_ticket_type(
            session, event.organizer_id, event.id, ticket_type_id, payload
        )


# --- registrations, orders, tickets ---


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
