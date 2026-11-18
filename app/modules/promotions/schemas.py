import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.modules.promotions.enums import DiscountType, PromoCodeStatus

# A custom code: letters, digits, "_" and "-". Stored upper-case, so "spring20" is "SPRING20".
CODE_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    discount_type: DiscountType
    # Percent (1-100) or a fixed amount in KZT, depending on discount_type.
    discount_value: int = Field(ge=1)
    # None means unlimited.
    max_redemptions: int | None = Field(default=None, ge=1)
    # Aware only: comparing naive and aware datetimes raises TypeError.
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        if self.discount_type == DiscountType.PERCENT and self.discount_value > 100:
            raise ValueError("A percent discount cannot be more than 100")
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("starts_at must be before ends_at")
        return self


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    name: str
    discount_type: DiscountType
    discount_value: int
    max_redemptions: int | None
    starts_at: datetime | None
    ends_at: datetime | None


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


class PromoCheckOut(BaseModel):
    """What a buyer needs to see before paying; the real discount is worked out at checkout."""

    code: str
    campaign_name: str
    discount_type: DiscountType
    discount_value: int


class PromoQrOut(BaseModel):
    """Where a scanned campaign QR leads: the event, and the code to pre-fill at checkout."""

    event_id: uuid.UUID
    code: str
