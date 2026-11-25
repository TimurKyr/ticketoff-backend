import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.common.pagination import Limit, Offset
from app.common.periods import ReportDate
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


PERIOD_NOTE = (
    "Optional period (R-118): `from_date` and `to_date` are days in Kazakhstan time "
    "(Asia/Almaty), both inclusive, either may be left out; from_date after to_date is 422. "
    "Sales, money and promo figures count orders by the order's date (a paid order is paid "
    "within its checkout hold, a free one is confirmed at once); refunds are those of these "
    "orders, so net = gross - refunds of the same orders. `checked_in` counts scans made in "
    "the period (undone scans left out), so check_in_percent may compare different tickets. "
    "Inventory (`capacity`, `tickets_total`, `remaining`, `percent_sold`) is always all-time. "
    "Without parameters the response is unchanged."
)


@router.get(
    "/overview",
    response_model=OverviewOut,
    summary="Totals over all my events",
    description=PERIOD_NOTE + " The event counts (`events`) are never narrowed.",
)
def overview(
    organizer: OrganizerDep,
    session: SessionDep,
    from_date: ReportDate = None,
    to_date: ReportDate = None,
) -> OverviewOut:
    return service.get_overview(session, organizer, from_date=from_date, to_date=to_date)


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
    description=PERIOD_NOTE
    + " With `ticket_type_id` (one of this event's types, else 404) every figure covers only "
    "that type: `ticket_types` has just that row, orders are those with such a ticket, "
    "tickets and check-ins are of that type, `tickets_total` is its quantity, and the money "
    "of an order with several types is shared out by line value (quantity x unit price), "
    "rounded down per order. Unused campaigns are still listed, with zeros.",
    responses={404: {"description": "Not my event, or the ticket type is not this event's"}},
)
def event_analytics(
    event: OwnedEventDep,
    session: SessionDep,
    from_date: ReportDate = None,
    to_date: ReportDate = None,
    ticket_type_id: Annotated[
        uuid.UUID | None, Query(description="Only this ticket type of the event")
    ] = None,
) -> EventAnalyticsOut:
    return service.get_event_analytics(
        session, event, from_date=from_date, to_date=to_date, ticket_type_id=ticket_type_id
    )
