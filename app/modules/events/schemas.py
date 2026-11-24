import enum
import uuid
from datetime import datetime
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.modules.events.enums import EventStatus, EventVisibility, SeatingType

# Buyers read it before paying; long enough for a real policy, short enough for a card.
REFUND_POLICY_MAX_LENGTH = 2000
# Integer column is 32-bit; the web form uses the same cap.
CAPACITY_MAX = 1_000_000


class EventBucket(enum.StrEnum):
    """Organizer dashboard grouping, derived from `status` and dates."""

    UPCOMING = "upcoming"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Availability(enum.StrEnum):
    """How much is left to buy, over the ticket types on sale (for event cards)."""

    AVAILABLE = "available"
    FEW_LEFT = "few_left"  # at most FEW_LEFT_TICKETS, or at most 10% of their quantity
    SOLD_OUT = "sold_out"


FEW_LEFT_TICKETS = 10
FEW_LEFT_PERCENT = 10


def availability_of(available: int, quantity_total: int) -> Availability:
    if available <= 0:
        return Availability.SOLD_OUT
    if available <= FEW_LEFT_TICKETS or available * 100 <= quantity_total * FEW_LEFT_PERCENT:
        return Availability.FEW_LEFT
    return Availability.AVAILABLE


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
    refund_policy: str | None = Field(default=None, max_length=REFUND_POLICY_MAX_LENGTH)
    category: str | None = Field(default=None, max_length=100)
    venue_id: uuid.UUID
    start_at: AwareDatetime
    end_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=CAPACITY_MAX)
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
    refund_policy: str | None = Field(default=None, max_length=REFUND_POLICY_MAX_LENGTH)
    category: str | None = Field(default=None, max_length=100)
    venue_id: uuid.UUID | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=CAPACITY_MAX)
    visibility: EventVisibility | None = None
    seating_type: SeatingType | None = None
    registration_opens_at: AwareDatetime | None = None
    registration_closes_at: AwareDatetime | None = None

    @field_validator("title", "venue_id", "start_at", "visibility", "seating_type", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        # Only runs for values that were actually sent, so omitting the field is fine.
        if value is None:
            raise ValueError("may not be null")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        check_dates(
            self.start_at, self.end_at, self.registration_opens_at, self.registration_closes_at
        )
        return self


CANCEL_REASON_MAX_LENGTH = 500


class EventCancel(BaseModel):
    """Optional body of POST /events/{id}/cancel (BE-37): why the event is called off.

    The reason goes into the event_cancelled email to buyers and into the audit row. Blank
    text counts as no reason.
    """

    reason: str | None = Field(default=None, max_length=CANCEL_REASON_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class VenueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    address: str | None
    city: str | None
    timezone: str


class CalendarLinks(BaseModel):
    """Add-to-calendar (BE-31). Nothing is written to anyone's calendar: these open the
    user's own calendar app or download a file."""

    google: str = Field(description="Google Calendar 'create event' page, times in UTC")
    outlook: str = Field(description="Outlook.com 'new event' page, times in UTC")
    ics: str = Field(
        description="GET /events/{id}/calendar.ics: iCalendar file in the venue's time zone"
    )


class EventOrganizerOut(BaseModel):
    """Who runs the event, as buyers see it. Never the phone, payout or verification data."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    contact_email: str | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organizer_id: uuid.UUID
    venue_id: uuid.UUID
    title: str
    slug: str | None = Field(
        description=(
            "Readable link: GET /events/{slug} works like GET /events/{id}. Null for private events"
        ),
    )
    description: str | None
    refund_policy: str | None
    category: str | None
    status: EventStatus
    visibility: EventVisibility
    seating_type: SeatingType
    paid_sales_active: bool
    sales_suspended: bool
    capacity: int | None
    start_at: datetime
    end_at: datetime | None
    registration_opens_at: datetime | None
    registration_closes_at: datetime | None
    # Over the ticket types GET /events/{id}/ticket-types/public lists right now (visible,
    # inside their sales window). Both null when no type is on sale.
    price_from: int | None = Field(
        default=None, description="Lowest price_kzt on sale; 0 if a free type is on sale"
    )
    availability: Availability | None = Field(
        default=None,
        description=(
            f"sold_out: nothing left; few_left: at most {FEW_LEFT_TICKETS} left or at most "
            f"{FEW_LEFT_PERCENT}% of the on-sale quantity"
        ),
    )
    calendar_links: CalendarLinks
    # Read from Event.organizer_public (set by annotate_on_sale), sent as "organizer".
    organizer: EventOrganizerOut | None = Field(
        default=None,
        validation_alias="organizer_public",
        serialization_alias="organizer",
        description="Who runs the event: display name and public contact email",
    )


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int
