import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.modules.ops.enums import NotificationType


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: NotificationType
    # Type-specific IDs, e.g. {"order_id": "..."}; the web app loads current data by them.
    payload: dict | None
    created_at: datetime
    # None while unread.
    read_at: datetime | None


class NotificationPage(BaseModel):
    """One page of my notifications, newest first.

    total counts the rows matching the filter; unread_count counts all my unread rows,
    whatever the filter (the web app's badge).
    """

    items: list[NotificationOut]
    total: int
    limit: int
    offset: int
    unread_count: int
