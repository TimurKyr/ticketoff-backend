import enum
import uuid
from datetime import datetime
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.modules.events.enums import EventStatus, EventVisibility, SeatingType


class EventBucket(enum.StrEnum):
    """Organizer dashboard grouping, derived from `status` and dates."""

    UPCOMING = "upcoming"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def check_dates(
    start_at: AwareDatetime | None,
    end_at: AwareDatetime | None,
    registration_opens_at: AwareDatetime | None,
    registration_closes_at: AwareDatetime | None,
) -> None:
    """Shared date rules; every argument may be None (nothing to compare)."""
    if start_at and end_at and end_at <= start_at:
        raise ValueError("end_at must be later than start_at")
    if start_at and registration_closes_at and registration_closes_at > start_at:
        raise ValueError("registration_closes_at must not be later than start_at")
    if (
        registration_opens_at
        and registration_closes_at
        and registration_closes_at < registration_opens_at
    ):
        raise ValueError("registration_closes_at must not be earlier than registration_opens_at")


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    venue_id: uuid.UUID
    start_at: AwareDatetime
    end_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1)
    visibility: EventVisibility = EventVisibility.PUBLIC
    seating_type: SeatingType = SeatingType.GENERAL
    registration_opens_at: AwareDatetime | None = None
    registration_closes_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        check_dates(
            self.start_at, self.end_at, self.registration_opens_at, self.registration_closes_at
        )
        return self


class EventUpdate(BaseModel):
    """Partial update: only the fields that are sent are changed."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    venue_id: uuid.UUID | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1)
    visibility: EventVisibility | None = None
    seating_type: SeatingType | None = None
    registration_opens_at: AwareDatetime | None = None
    registration_closes_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        check_dates(
            self.start_at, self.end_at, self.registration_opens_at, self.registration_closes_at
        )
        return self


class VenueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    address: str | None
    city: str | None
    timezone: str


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organizer_id: uuid.UUID
    venue_id: uuid.UUID
    title: str
    description: str | None
    category: str | None
    status: EventStatus
    visibility: EventVisibility
    seating_type: SeatingType
    paid_sales_active: bool
    capacity: int | None
    start_at: datetime
    end_at: datetime | None
    registration_opens_at: datetime | None
    registration_closes_at: datetime | None


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int
