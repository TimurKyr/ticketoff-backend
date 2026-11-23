import enum
import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field

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
    # The event was called off: nobody is admitted, whatever the ticket.
    EVENT_CANCELLED = "event_cancelled"


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


class AttendeePage(BaseModel):
    items: list[AttendeeRow]
    total: int
    limit: int
    offset: int


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
    venue_name: str
    venue_address: str | None
    # IANA name, e.g. "Asia/Almaty": show start_at in the venue's local time.
    timezone: str
    is_owner: bool


# --- offline mode (BE-32) -------------------------------------------------------------------

# How a scanner gets ticket_id from a QR without the network (the signing secret never
# leaves the server): a ticket QR is "tk1.<ticket_uuid>.<signature>", so ticket_id is the text
# between the first and the last dot. A QR starting with "pc1." or containing "promo=pc1." is
# a campaign QR, not a ticket: reject it on the device too. Offline, admit only a ticket_id
# that is in the pack with status "valid" and was not scanned on this device yet.
OFFLINE_QR_HOWTO = (
    "Getting `ticket_id` from a QR offline: a ticket QR is `tk1.<ticket_uuid>.<signature>`, "
    "so `ticket_id` is the part between the first and the last dot. A QR that starts with "
    "`pc1.` or contains `promo=pc1.` is a campaign QR, not a ticket: reject it offline too. "
    "The signing secret is never sent to devices, so offline the check is: `ticket_id` is in "
    "the pack, its status is `valid`, and this device has not scanned it yet."
)


class OfflineTicket(BaseModel):
    ticket_id: uuid.UUID
    status: TicketStatus
    attendee_name: str
    ticket_type: str


class OfflinePackOut(BaseModel):
    event_id: uuid.UUID
    event_title: str
    event_status: EventStatus
    # Server time of the download: show it on the device ("pack from 18:40").
    generated_at: datetime
    checked_in_count: int
    total_count: int
    tickets: list[OfflineTicket]


class SyncItemIn(BaseModel):
    # The ticket UUID taken from the QR. The whole "tk1." QR text is accepted too (its
    # signature is then checked); a campaign "pc1." code gives campaign_qr_rejected.
    ticket_id: str = Field(min_length=1, max_length=2048)
    # When the device scanned it; must carry a timezone (e.g. "2026-11-26T13:05:00Z").
    # A time in the future (device clock ahead) is recorded as the time of the sync.
    scanned_at: AwareDatetime
    device_id: str = Field(min_length=1, max_length=100)


class SyncIn(BaseModel):
    event_id: uuid.UUID
    items: list[SyncItemIn] = Field(min_length=1, max_length=1000)


class SyncItemOut(BaseModel):
    ticket_id: str  # as sent
    device_id: str
    result: ScanResult
    ticket: ScannedTicket | None = None
    # For checked_in and already_used: when the ticket was actually checked in, by whom
    # (staff email) and on which device (null for a scan made online or by hand).
    checked_in_at: datetime | None = None
    checked_in_by: str | None = None
    checked_in_device_id: str | None = None


class SyncOut(BaseModel):
    items: list[SyncItemOut]  # in the order they were sent
    checked_in_count: int
    total_count: int
