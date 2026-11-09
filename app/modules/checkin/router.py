import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.common.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.modules.checkin import service
from app.modules.checkin.schemas import (
    AttendeeRow,
    MyEventOut,
    ScanIn,
    ScannedTicket,
    ScanOut,
    StaffIn,
    StaffOut,
    StatsOut,
)
from app.modules.identity.dependencies import CurrentUser, SessionDep

checkin_router = APIRouter(prefix="/checkin", tags=["checkin"])
staff_router = APIRouter(prefix="/events/{event_id}/staff", tags=["event-staff"])


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


@staff_router.post(
    "", response_model=StaffOut, status_code=status.HTTP_201_CREATED, summary="Assign staff"
)
def add_staff(
    event_id: uuid.UUID, payload: StaffIn, user: CurrentUser, session: SessionDep
) -> StaffOut:
    with _http_errors():
        return service.add_staff(session, user, event_id, payload.email)


@staff_router.get("", response_model=list[StaffOut], summary="Event staff")
def list_staff(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> list[StaffOut]:
    with _http_errors():
        return service.list_staff(session, user, event_id)


@staff_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove staff")
def remove_staff(
    event_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    with _http_errors():
        service.remove_staff(session, user, event_id, user_id)


@checkin_router.get("/my-events", response_model=list[MyEventOut], summary="Events I can scan")
def my_events(user: CurrentUser, session: SessionDep) -> list[MyEventOut]:
    return service.my_events(session, user)


@checkin_router.post("/scan", response_model=ScanOut, summary="Scan a ticket QR at the door")
def scan(payload: ScanIn, user: CurrentUser, session: SessionDep) -> ScanOut:
    """Always 200 for a scan the caller may make; the outcome is in `result`."""
    with _http_errors():
        return service.scan(session, user, payload.event_id, payload.qr_token)


@checkin_router.post(
    "/tickets/{ticket_id}/undo", response_model=ScannedTicket, summary="Undo a mistaken check-in"
)
def undo(ticket_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ScannedTicket:
    with _http_errors():
        return service.undo_check_in(session, user, ticket_id)


@checkin_router.get(
    "/events/{event_id}/attendees", response_model=list[AttendeeRow], summary="Find a guest"
)
def attendees(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AttendeeRow]:
    with _http_errors():
        return service.search_attendees(session, user, event_id, q, limit)


@checkin_router.get("/events/{event_id}/stats", response_model=StatsOut, summary="Door stats")
def stats(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> StatsOut:
    with _http_errors():
        return service.stats(session, user, event_id)
