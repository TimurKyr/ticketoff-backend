import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.common.errors import (
    BusinessRuleError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
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
from app.modules.identity.dependencies import require_organizer
from app.modules.identity.models import OrganizerProfile

SessionDep = Annotated[Session, Depends(get_session)]
OrganizerDep = Annotated[OrganizerProfile, Depends(require_organizer)]
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]

venues_router = APIRouter(prefix="/venues", tags=["venues"])
events_router = APIRouter(prefix="/events", tags=["events"])
organizer_router = APIRouter(prefix="/organizer/events", tags=["organizer-events"])


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
    except InvalidInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


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
    with _http_errors():
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
    with _http_errors():
        return service.get_public_event(session, event_id)


@events_router.patch("/{event_id}", response_model=EventOut, summary="Edit an event")
def update_event(payload: EventUpdate, event: OwnedEventDep, session: SessionDep) -> Event:
    with _http_errors():
        return service.update_event(session, event, payload)


@events_router.post(
    "/{event_id}/duplicate",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate an event into a new draft",
)
def duplicate_event(event: OwnedEventDep, session: SessionDep) -> Event:
    with _http_errors():
        return service.duplicate_event(session, event)


@events_router.post("/{event_id}/publish", response_model=EventOut, summary="Publish a draft")
def publish_event(event: OwnedEventDep, session: SessionDep) -> Event:
    with _http_errors():
        return service.publish_event(session, event)


@events_router.post("/{event_id}/unpublish", response_model=EventOut, summary="Back to draft")
def unpublish_event(event: OwnedEventDep, session: SessionDep) -> Event:
    with _http_errors():
        return service.unpublish_event(session, event)


@events_router.post("/{event_id}/cancel", response_model=EventOut, summary="Cancel an event")
def cancel_event(event: OwnedEventDep, session: SessionDep) -> Event:
    with _http_errors():
        return service.cancel_event(session, event)


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
