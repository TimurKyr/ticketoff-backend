"""Platform admin API (BE-22, BE-26). The actions return the changed entity."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.identity.enums import SystemRole
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus

SearchType = Literal["user", "event", "order"]


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    system_role: SystemRole
    email_verified: bool
    is_suspended: bool


class SearchItemOut(BaseModel):
    """One search hit.

    user: label = email, status = system role, suspended = users.is_suspended.
    event: label = title, status = event status, suspended = events.sales_suspended.
    order: label = "buyer email · amount KZT · status" (amount = what the buyer pays),
    status = order status, suspended = sales_suspended of the order's event.
    """

    type: SearchType
    id: uuid.UUID
    label: str
    status: str
    suspended: bool


class SearchPage(BaseModel):
    items: list[SearchItemOut]
    total: int
    limit: int
    offset: int


class AdminPaymentOut(BaseModel):
    """event_id: the activation's event, or the event of a ticket payment's order.
    event_title: that event's title (BE-37), null when there is no event."""

    id: uuid.UUID
    purpose: PaymentPurpose
    status: PaymentStatus
    order_id: uuid.UUID | None
    event_id: uuid.UUID | None
    event_title: str | None = None
    amount_kzt: int
    is_simulated: bool
    created_at: datetime


class PaymentPage(BaseModel):
    items: list[AdminPaymentOut]
    total: int
    limit: int
    offset: int


class AdminRefundOut(BaseModel):
    id: uuid.UUID
    payment_id: uuid.UUID
    order_id: uuid.UUID | None
    event_id: uuid.UUID | None
    event_title: str | None = None
    amount_kzt: int
    status: RefundStatus
    reason: str | None
    created_at: datetime


class RefundPage(BaseModel):
    items: list[AdminRefundOut]
    total: int
    limit: int
    offset: int


class PlatformSettingsOut(BaseModel):
    """Platform fees. Saved by a platform admin (PATCH /admin/settings); until then the
    server configuration (env). updated_at is null while the env values apply."""

    activation_fee_kzt: int
    processing_fee_percent: int
    checkout_hold_minutes: int
    currency: Literal["KZT"] = "KZT"
    read_only: Literal[False] = False
    updated_at: datetime | None = None


class PlatformSettingsUpdate(BaseModel):
    """Only the fields sent change. Bounds as in the server configuration. A new activation
    fee applies to activations paid from now on; paid ones keep their amount."""

    model_config = ConfigDict(extra="forbid")

    activation_fee_kzt: int | None = Field(default=None, ge=1, le=10_000_000)
    processing_fee_percent: int | None = Field(default=None, ge=0, le=100)
    checkout_hold_minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def _something_to_change(self) -> "PlatformSettingsUpdate":
        if not self.model_fields_set or any(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("Send at least one setting, none of them null")
        return self


class PromoActivityOut(BaseModel):
    """One campaign's redemptions in the period, on confirmed (paid or free) orders.

    gross_kzt is what those orders were paid (after the discount)."""

    event_id: uuid.UUID
    event_title: str
    campaign_id: uuid.UUID
    campaign_name: str
    codes: int
    redemptions: int
    discount_total_kzt: int
    gross_kzt: int


class PromoActivityPage(BaseModel):
    """total_redemptions and total_discount_kzt cover every page, not only this one."""

    items: list[PromoActivityOut]
    total: int
    limit: int
    offset: int
    total_redemptions: int
    total_discount_kzt: int
