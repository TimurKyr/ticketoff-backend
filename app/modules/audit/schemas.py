import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TimelineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    action_type: str
    entity_type: str
    entity_id: uuid.UUID | None
    # None for system actions.
    actor_user_id: uuid.UUID | None
    details: dict | None


class TimelinePage(BaseModel):
    """One page of an event's audit log, oldest first. total counts every matching row."""

    items: list[TimelineItemOut]
    total: int
    limit: int
    offset: int
