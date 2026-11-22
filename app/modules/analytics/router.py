from typing import Annotated

from fastapi import APIRouter, Depends

from app.common.pagination import Limit, Offset
from app.modules.analytics import service
from app.modules.analytics.schemas import EventAnalyticsOut, EventHistoryPage, OverviewOut
from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.events.schemas import EventBucket
from app.modules.identity.dependencies import SessionDep, require_organizer
from app.modules.identity.models import OrganizerProfile

router = APIRouter(prefix="/organizer/analytics", tags=["analytics"])

OrganizerDep = Annotated[OrganizerProfile, Depends(require_organizer)]
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]


@router.get("/overview", response_model=OverviewOut, summary="Totals over all my events")
def overview(organizer: OrganizerDep, session: SessionDep) -> OverviewOut:
    return service.get_overview(session, organizer)


@router.get(
    "/events",
    response_model=EventHistoryPage,
    summary="Event history: my events, newest first, with their numbers",
    description="The per-event action log (audit) is GET /events/{event_id}/timeline.",
)
def event_history(
    organizer: OrganizerDep,
    session: SessionDep,
    bucket: EventBucket | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> EventHistoryPage:
    return service.list_history(session, organizer, bucket=bucket, limit=limit, offset=offset)


@router.get(
    "/events/{event_id}",
    response_model=EventAnalyticsOut,
    summary="One of my events: numbers, ticket types, promo campaigns and sales per day",
)
def event_analytics(event: OwnedEventDep, session: SessionDep) -> EventAnalyticsOut:
    return service.get_event_analytics(session, event)
