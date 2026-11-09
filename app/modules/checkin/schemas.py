import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.modules.events.enums import EventStatus
from app.modules.identity.enums import StaffRole
from app.modules.orders.enums import TicketStatus


class ScanResult(enum.StrEnum):
    CHECKED_IN = "checked_in"
    ALREADY_USED = "already_used"
    INVALID = "invalid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    WRONG_EVENT = "wrong_event"
    CAMPAIGN_QR_REJECTED = "campaign_qr_rejected"


class ScanIn(BaseModel):
    event_id: uuid.UUID
    qr_token: str = Field(min_length=1, max_length=2048)


class ScannedTicket(BaseModel):
    id: uuid.UUID
    attendee_name: str
    ticket_type: str
    seat_id: uuid.UUID | None
    status: TicketStatus


class ScanOut(BaseModel):
    result: ScanResult
    ticket: ScannedTicket | None = None
    checked_in_count: int
    total_count: int


class AttendeeRow(BaseModel):
    ticket_id: uuid.UUID
    attendee_name: str
    attendee_email: str | None
    ticket_type: str
    status: TicketStatus


class StatsOut(BaseModel):
    registered: int
    checked_in: int
    absent: int
    percent: float


class StaffIn(BaseModel):
    email: EmailStr


class StaffOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: StaffRole


class MyEventOut(BaseModel):
    """An event the current user may scan tickets for."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: EventStatus
    start_at: datetime
    end_at: datetime | None
    is_owner: bool
