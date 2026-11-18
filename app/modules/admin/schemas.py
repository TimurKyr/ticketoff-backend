"""Platform admin API (BE-22). Read models only; the actions return the changed entity."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.identity.enums import SystemRole
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus

SearchType = Literal["user", "event"]


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    system_role: SystemRole
    email_verified: bool
    is_suspended: bool


class SearchItemOut(BaseModel):
    """One search hit. label is the user's email or the event's title; status is the
    user's system role or the event's status; suspended is users.is_suspended or
    events.sales_suspended."""

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
    """event_id: the activation's event, or the event of a ticket payment's order."""

    id: uuid.UUID
    purpose: PaymentPurpose
    status: PaymentStatus
    order_id: uuid.UUID | None
    event_id: uuid.UUID | None
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
    """Read-only: these come from the server configuration (env), not from the database."""

    activation_fee_kzt: int
    processing_fee_percent: int
    checkout_hold_minutes: int
    currency: Literal["KZT"] = "KZT"
    read_only: Literal[True] = True
