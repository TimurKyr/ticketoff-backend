import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.common.pagination import Limit, Offset
from app.core.db import get_session
from app.modules.events import service
from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.events.schemas import (
    EventBucket,
    EventCreate,
    EventOut,
    EventPage,
    EventUpdate,
    VenueOut,
)
from app.modules.identity.dependencies import CurrentUser, require_organizer
from app.modules.identity.models import OrganizerProfile
from app.modules.payments import service as payments_service

SessionDep = Annotated[Session, Depends(get_session)]
OrganizerDep = Annotated[OrganizerProfile, Depends(require_organizer)]
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]

venues_router = APIRouter(prefix="/venues", tags=["venues"])
events_router = APIRouter(prefix="/events", tags=["events"])
organizer_router = APIRouter(prefix="/organizer/events", tags=["organizer-events"])


@venues_router.get("", response_model=list[VenueOut], summary="List venues")
def list_venues(session: SessionDep) -> list:
    return service.list_venues(session)


@events_router.post(
    "",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft event",
)
def create_event(payload: EventCreate, organizer: OrganizerDep, session: SessionDep) -> Event:
    return service.create_event(session, organizer, payload)


@events_router.get("", response_model=EventPage, summary="Public list of published events")
def list_public_events(
    session: SessionDep,
    q: str | None = None,
    category: str | None = None,
    from_date: AwareDatetime | None = None,
    to_date: AwareDatetime | None = None,
    include_past: bool = False,
    limit: Limit = 20,
    offset: Offset = 0,
) -> EventPage:
    items, total = service.list_public_events(
        session,
        q=q,
        category=category,
        from_date=from_date,
        to_date=to_date,
        include_past=include_past,
        limit=limit,
        offset=offset,
    )
    return EventPage(items=items, total=total, limit=limit, offset=offset)


@events_router.get("/{event_id}", response_model=EventOut, summary="Public event page")
def get_event(event_id: uuid.UUID, session: SessionDep) -> Event:
    return service.get_public_event(session, event_id)


@events_router.patch("/{event_id}", response_model=EventOut, summary="Edit an event")
def update_event(
    payload: EventUpdate, event: OwnedEventDep, user: CurrentUser, session: SessionDep
) -> Event:
    return service.update_event(session, event, payload, user)


@events_router.post(
    "/{event_id}/duplicate",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate an event into a new draft",
)
def duplicate_event(event: OwnedEventDep, session: SessionDep) -> Event:
    return service.duplicate_event(session, event)


@events_router.post("/{event_id}/publish", response_model=EventOut, summary="Publish a draft")
def publish_event(event: OwnedEventDep, user: CurrentUser, session: SessionDep) -> Event:
    return service.publish_event(session, event, user)


@events_router.post("/{event_id}/unpublish", response_model=EventOut, summary="Back to draft")
def unpublish_event(event: OwnedEventDep, user: CurrentUser, session: SessionDep) -> Event:
    return service.unpublish_event(session, event, user)


@events_router.post(
    "/{event_id}/cancel",
    response_model=EventOut,
    summary="Cancel an event: its orders are cancelled and paid ones refunded",
)
def cancel_event(event: OwnedEventDep, user: CurrentUser, session: SessionDep) -> Event:
    """One transaction: free and pending orders cancelled, paid orders fully refunded."""
    return payments_service.cancel_event_with_orders(session, event, user)


@organizer_router.get("", response_model=EventPage, summary="My events, optionally by bucket")
def list_my_events(
    organizer: OrganizerDep,
    session: SessionDep,
    bucket: EventBucket | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> EventPage:
    items, total = service.list_organizer_events(
        session, organizer, bucket=bucket, limit=limit, offset=offset
    )
    return EventPage(items=items, total=total, limit=limit, offset=offset)


@organizer_router.get("/{event_id}", response_model=EventOut, summary="My event, any status")
def get_my_event(event: OwnedEventDep) -> Event:
    return event
