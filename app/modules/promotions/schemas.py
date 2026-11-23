import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.modules.orders.schemas import PRICE_KZT_MAX, QUANTITY_MAX, RegistrationItemIn
from app.modules.promotions.enums import DiscountType, PromoCodeStatus

# A custom code: letters, digits, "_" and "-". Stored upper-case, so "spring20" is "SPRING20".
CODE_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"
# More ticket types than an event will ever have; keeps a request (and its IN list) small.
TICKET_TYPE_IDS_MAX = 100


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    discount_type: DiscountType
    # Percent (1-100) or a fixed amount in KZT, depending on discount_type. Both columns are
    # 32-bit: the caps (the same as for ticket types) make a huge value a 422, not a 500.
    discount_value: int = Field(ge=1, le=PRICE_KZT_MAX)
    # None means unlimited.
    max_redemptions: int | None = Field(default=None, ge=1, le=QUANTITY_MAX)
    # Aware only: comparing naive and aware datetimes raises TypeError.
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    # The ticket types the codes apply to (BE-29), all of the campaign's event (hidden ones
    # too). Empty or left out: every type. Duplicates are dropped.
    ticket_type_ids: list[uuid.UUID] = Field(default_factory=list, max_length=TICKET_TYPE_IDS_MAX)

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        self.ticket_type_ids = list(dict.fromkeys(self.ticket_type_ids))
        if self.discount_type == DiscountType.PERCENT and self.discount_value > 100:
            raise ValueError("A percent discount cannot be more than 100")
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("starts_at must be before ends_at")
        return self


class CampaignOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    name: str
    discount_type: DiscountType
    discount_value: int
    max_redemptions: int | None
    starts_at: datetime | None
    ends_at: datetime | None
    # Sorted; empty means the codes apply to every ticket type of the event.
    ticket_type_ids: list[uuid.UUID]


class CodeCreate(BaseModel):
    # Leave it out and the server makes one (8 easy-to-read characters).
    code: str | None = Field(default=None, pattern=CODE_PATTERN)


class CodeUpdate(BaseModel):
    # Only the organizer's own switch. "exhausted" and "expired" are worked out, not set.
    status: Literal[PromoCodeStatus.ACTIVE, PromoCodeStatus.DISABLED]


class CodeOut(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    code: str
    # The campaign QR token ("pc1....") and the status as of now:
    # active, disabled, exhausted (all uses taken) or expired (past ends_at).
    qr_token: str
    status: PromoCodeStatus
    # True only when the status is active and the campaign has already started.
    redeemable_now: bool
    redemptions: int


class PromoCheckIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    # Optional (BE-29): the tickets the buyer is about to take. With them, the answer has the
    # discount on exactly these tickets, and a code that applies to none of them is a 409.
    items: list[RegistrationItemIn] | None = Field(default=None, max_length=TICKET_TYPE_IDS_MAX)


class PromoCheckOut(BaseModel):
    """What a buyer needs to see before paying; the real discount is worked out at checkout."""

    code: str
    campaign_name: str
    discount_type: DiscountType
    discount_value: int
    # The ticket types the code applies to, sorted; empty means every type of the event.
    ticket_type_ids: list[uuid.UUID]
    # Only when `items` were sent: the price of the matching items, and the discount on them
    # (the same calculation as checkout). None without items.
    eligible_subtotal_kzt: int | None = None
    discount_kzt: int | None = None


class PromoQrOut(BaseModel):
    """Where a scanned campaign QR leads: the event, and the code to pre-fill at checkout."""

    event_id: uuid.UUID
    code: str
