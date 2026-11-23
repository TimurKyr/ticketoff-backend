import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.common.pagination import Limit, Offset
from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.identity.dependencies import (
    EMAIL_NOT_VERIFIED_DOC,
    CurrentUser,
    SessionDep,
    VerifiedUser,
)
from app.modules.orders import service
from app.modules.orders.enums import OrderStatus
from app.modules.orders.schemas import (
    CheckoutCreate,
    EventInventoryOut,
    EventOrderPage,
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
event_orders_router = APIRouter(prefix="/events", tags=["orders"])
tickets_router = APIRouter(prefix="/tickets", tags=["tickets"])

# The path's event, if the logged-in organizer owns it; otherwise 404 (403 if not an organizer).
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]


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
    return service.create_ticket_type(session, event.organizer_id, event.id, payload)


@ticket_types_router.get(
    "/{event_id}/ticket-types",
    response_model=list[TicketTypeOut],
    summary="Ticket types of my event, hidden ones included",
)
def list_ticket_types(event: OwnedEventDep, session: SessionDep) -> list[TicketTypeOut]:
    return service.list_ticket_types(session, event.organizer_id, event.id)


@ticket_types_router.get(
    "/{event_id}/inventory",
    response_model=EventInventoryOut,
    summary="Inventory of my event: sold, reserved, available and ticket statuses",
)
def get_inventory(event: OwnedEventDep, session: SessionDep) -> EventInventoryOut:
    return service.get_inventory(session, event)


# Declared before any GET /{ticket_type_id} route, so "public" is never parsed as an id.
@ticket_types_router.get(
    "/{event_id}/ticket-types/public",
    response_model=list[PublicTicketTypeOut],
    summary="Ticket types on sale now (no login)",
)
def list_public_ticket_types(event_id: uuid.UUID, session: SessionDep) -> list[PublicTicketTypeOut]:
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
    user: CurrentUser,
    session: SessionDep,
) -> TicketTypeOut:
    return service.update_ticket_type(
        session, event.organizer_id, event.id, ticket_type_id, payload, actor_user_id=user.id
    )


# --- the organizer's orders and guests (BE-30) ---


@event_orders_router.get(
    "/{event_id}/orders",
    response_model=EventOrderPage,
    summary="Orders and guests of my event, newest first",
)
def list_event_orders(
    event: OwnedEventDep,
    user: CurrentUser,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(
            max_length=100,
            description="Part of the buyer's email or a guest's name or email (any case), "
            "a full order id, or the start of one (4+ hex digits)",
        ),
    ] = None,
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> EventOrderPage:
    """can_refund / can_cancel follow the same rules as POST /orders/{id}/refund and
    POST /orders/{id}/cancel. A pending order keeps its status after its hold ran out;
    hold_expired tells it apart."""
    return service.list_event_orders(
        session, user, event, q=q, status=order_status, limit=limit, offset=offset
    )


# --- registrations, orders, tickets ---


@registrations_router.post(
    "/{event_id}/registrations",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register for free tickets",
    responses={403: {"description": EMAIL_NOT_VERIFIED_DOC}},
)
def register_free(
    event_id: uuid.UUID, payload: RegistrationCreate, user: VerifiedUser, session: SessionDep
) -> OrderOut:
    return service.register_free(session, user, event_id, payload)


@registrations_router.post(
    "/{event_id}/checkout",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a checkout for paid tickets (simulated payment follows)",
    responses={403: {"description": EMAIL_NOT_VERIFIED_DOC}},
)
def checkout(
    event_id: uuid.UUID, payload: CheckoutCreate, user: VerifiedUser, session: SessionDep
) -> OrderOut:
    return service.checkout(session, user, event_id, payload)


# Declared before /{order_id} so "me" is not parsed as an order id.
@orders_router.get("/me", response_model=list[OrderOut], summary="My orders")
def list_my_orders(user: CurrentUser, session: SessionDep) -> list[OrderOut]:
    return service.list_my_orders(session, user)


@orders_router.get("/{order_id}", response_model=OrderOut, summary="One of my orders")
def get_my_order(order_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> OrderOut:
    return service.get_my_order(session, user, order_id)


@orders_router.post(
    "/{order_id}/cancel",
    response_model=OrderOut,
    summary="Cancel a free registration (buyer or organizer) or my pending checkout",
)
def cancel_order(order_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> OrderOut:
    """Paid orders are refunded by the organizer instead (409 here)."""
    return service.cancel_order(session, user, order_id)


@tickets_router.get(
    "/{ticket_id}", response_model=TicketOut, summary="One of my tickets, with its QR token"
)
def get_my_ticket(ticket_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> TicketOut:
    return service.get_my_ticket(session, user, ticket_id)


@tickets_router.get(
    "/{ticket_id}/pdf",
    response_class=Response,
    responses={
        200: {
            "description": "Printable A4 ticket with the entry QR code",
            "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
        },
        404: {"description": "Ticket not found (or not mine)"},
        409: {"description": "Ticket is no longer valid, or the event was cancelled"},
    },
    summary="One of my tickets as a printable PDF",
)
def get_my_ticket_pdf(ticket_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Response:
    content, filename = service.get_my_ticket_pdf(session, user, ticket_id)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The QR is an entry credential: browsers and proxies must not keep a copy.
            "Cache-Control": "no-store",
        },
    )
