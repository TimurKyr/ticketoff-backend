import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.common.pagination import Limit, Offset
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.notifications import service
from app.modules.notifications.schemas import NotificationOut, NotificationPage

# Only list and mark-read: notifications are created by the flows, never by the client, and
# are kept (no PATCH or DELETE).
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])


@notifications_router.get("", response_model=NotificationPage, summary="My notifications")
def list_notifications(
    user: CurrentUser,
    session: SessionDep,
    unread: Annotated[bool, Query(description="Only unread notifications")] = False,
    limit: Limit = 20,
    offset: Offset = 0,
) -> NotificationPage:
    return service.list_my_notifications(
        session, user, unread_only=unread, limit=limit, offset=offset
    )


@notifications_router.post(
    "/{notification_id}/read",
    response_model=NotificationOut,
    summary="Mark one of my notifications read",
)
def read_notification(
    notification_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> NotificationOut:
    return service.mark_my_notification_read(session, user, notification_id)
