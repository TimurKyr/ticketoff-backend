from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from app.common.pagination import Limit, Offset
from app.modules.audit import service
from app.modules.audit.schemas import TimelinePage
from app.modules.events.dependencies import require_event_owner_or_admin
from app.modules.events.models import Event
from app.modules.identity.dependencies import SessionDep

# The path's event, for its organizer or a platform admin (BE-26); anyone else gets 404.
OwnedEventDep = Annotated[Event, Depends(require_event_owner_or_admin)]

# Read-only on purpose: the audit log is append-only, so there is no PATCH or DELETE.
timeline_router = APIRouter(prefix="/events", tags=["audit"])


@timeline_router.get(
    "/{event_id}/timeline",
    response_model=TimelinePage,
    summary="Audit log of my event (any event for a platform admin), oldest first",
)
def get_timeline(
    event: OwnedEventDep,
    session: SessionDep,
    from_date: AwareDatetime | None = None,
    to_date: AwareDatetime | None = None,
    action_type: Annotated[
        str | None,
        Query(
            max_length=100,
            description='Exact action type, or a prefix ending in "." (e.g. "order.")',
        ),
    ] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> TimelinePage:
    return service.get_timeline(
        session,
        event.id,
        from_date=from_date,
        to_date=to_date,
        action_type=action_type,
        limit=limit,
        offset=offset,
    )
