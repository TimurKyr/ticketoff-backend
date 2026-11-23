import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.modules.checkin import service
from app.modules.checkin.schemas import (
    OFFLINE_QR_HOWTO,
    AttendeePage,
    MyEventOut,
    OfflinePackOut,
    ScanIn,
    ScannedTicket,
    ScanOut,
    StaffIn,
    StaffOut,
    StatsOut,
    SyncIn,
    SyncOut,
)
from app.modules.identity.dependencies import CurrentUser, SessionDep

checkin_router = APIRouter(prefix="/checkin", tags=["checkin"])
staff_router = APIRouter(prefix="/events/{event_id}/staff", tags=["event-staff"])


@staff_router.post(
    "", response_model=StaffOut, status_code=status.HTTP_201_CREATED, summary="Assign staff"
)
def add_staff(
    event_id: uuid.UUID, payload: StaffIn, user: CurrentUser, session: SessionDep
) -> StaffOut:
    return service.add_staff(session, user, event_id, payload.email)


@staff_router.get("", response_model=list[StaffOut], summary="Event staff")
def list_staff(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> list[StaffOut]:
    return service.list_staff(session, user, event_id)


@staff_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove staff")
def remove_staff(
    event_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    service.remove_staff(session, user, event_id, user_id)


@checkin_router.get("/my-events", response_model=list[MyEventOut], summary="Events I can scan")
def my_events(user: CurrentUser, session: SessionDep) -> list[MyEventOut]:
    return service.my_events(session, user)


@checkin_router.post("/scan", response_model=ScanOut, summary="Scan a ticket QR at the door")
def scan(payload: ScanIn, user: CurrentUser, session: SessionDep) -> ScanOut:
    """Always 200 for a scan the caller may make; the outcome is in `result`."""
    return service.scan(session, user, payload.event_id, payload.qr_token)


@checkin_router.post(
    "/tickets/{ticket_id}/check-in",
    response_model=ScanOut,
    summary="Check in a guest by ticket id",
    responses={404: {"description": "Unknown ticket, or one the caller may not scan"}},
)
def check_in(ticket_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ScanOut:
    """Manual check-in of a guest found via /attendees; same results as /scan, always 200."""
    return service.check_in_ticket(session, user, ticket_id)


@checkin_router.post(
    "/tickets/{ticket_id}/undo", response_model=ScannedTicket, summary="Undo a mistaken check-in"
)
def undo(ticket_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ScannedTicket:
    return service.undo_check_in(session, user, ticket_id)


@checkin_router.get(
    "/events/{event_id}/attendees", response_model=AttendeePage, summary="Find a guest"
)
def attendees(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AttendeePage:
    return service.search_attendees(session, user, event_id, q, limit, offset)


@checkin_router.get("/events/{event_id}/stats", response_model=StatsOut, summary="Door stats")
def stats(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> StatsOut:
    return service.stats(session, user, event_id)


@checkin_router.get(
    "/events/{event_id}/offline-pack",
    response_model=OfflinePackOut,
    summary="Download tickets for offline scanning",
    description=(
        "For the event owner or assigned staff (404 otherwise): every ticket of the event with "
        "its status, attendee name and ticket type, plus `generated_at` (server time). Store it "
        "on the device and scan against it while there is no network; send the scans later "
        "with `POST /checkin/sync`. Download it again shortly before the doors open.\n\n"
        + OFFLINE_QR_HOWTO
    ),
)
def offline_pack(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> OfflinePackOut:
    return service.offline_pack(session, user, event_id)


@checkin_router.post(
    "/sync",
    response_model=SyncOut,
    summary="Upload check-ins made offline",
    description=(
        "Sends the scans a device made offline. Each item is checked in exactly like "
        "`/scan` (one transaction per item), with `scanned_at` as the check-in time (never "
        "later than now). One result per item, in the sent order: `checked_in`; "
        "`already_used` with `checked_in_at`, `checked_in_by` and `checked_in_device_id` of "
        "the check-in that won (another device or gate); `cancelled` / `refunded` if the "
        "ticket was cancelled after the pack was downloaded; `invalid`, `wrong_event`, "
        "`campaign_qr_rejected`, `event_cancelled`. Always 200 for an event the caller may "
        "scan (404 otherwise). Resending is safe: an item already applied comes back as "
        "`already_used` with this device's own `checked_in_device_id`.\n\n" + OFFLINE_QR_HOWTO
    ),
)
def sync(payload: SyncIn, user: CurrentUser, session: SessionDep) -> SyncOut:
    return service.sync(session, user, payload)
