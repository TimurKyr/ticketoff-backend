import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.events.enums import EventStatus
from app.modules.orders.enums import TicketKind
from app.modules.promotions.enums import DiscountType


class SalesFigures(BaseModel):
    """Sales numbers of one event, or the sum over several. Money is whole KZT, simulated."""

    orders: int = Field(description="Paid or free-confirmed orders.")
    tickets_sold: int = Field(description="Tickets in those orders (refunded orders excluded).")
    checked_in: int = Field(description="Tickets scanned at the door (in the period, if any).")
    check_in_percent: float = Field(description="checked_in / tickets_sold, 0 when none sold.")
    gross_kzt: int = Field(
        description="Succeeded ticket payments, i.e. already after promo discounts."
    )
    discounts_kzt: int = Field(
        description="Promo discounts given on those orders: what the promos cost the organizer."
    )
    fees_kzt: int
    refunds_kzt: int
    estimated_payout_kzt: int = Field(
        description="gross_kzt - fees_kzt - refunds_kzt, the same rule as the payment summary."
    )


class EventStatsOut(SalesFigures):
    event_id: uuid.UUID
    title: str
    status: EventStatus
    start_at: datetime
    end_at: datetime | None
    capacity: int | None
    tickets_total: int = Field(
        description="Sum of quantity_total over all ticket types (capacity may be empty)."
    )
    remaining: int = Field(
        description="tickets_total - tickets sold ever, never below 0 (ignores the period)."
    )
    percent_sold: float = Field(
        description="Tickets sold ever / tickets_total, 0 without tickets (ignores the period)."
    )


class EventHistoryPage(BaseModel):
    items: list[EventStatsOut]
    total: int
    limit: int
    offset: int
    fee_percent: int
    currency: Literal["KZT"] = "KZT"
    is_simulated: Literal[True] = True


class EventCounts(BaseModel):
    """How many of my events fall in each dashboard bucket.

    Every event is in exactly one bucket, so total = upcoming + active + completed + cancelled.
    `draft` is not a bucket: it says how many of them are still unpublished drafts.
    """

    total: int
    upcoming: int
    active: int
    completed: int
    cancelled: int
    draft: int


class OverviewOut(BaseModel):
    events: EventCounts
    totals: SalesFigures
    fee_percent: int
    currency: Literal["KZT"] = "KZT"
    is_simulated: Literal[True] = True


class TicketTypeStatsOut(BaseModel):
    ticket_type_id: uuid.UUID
    name: str
    kind: TicketKind
    price_kzt: int
    quantity_total: int
    sold: int
    sales_kzt: int = Field(
        description="sum of unit price x quantity of the sold tickets, before promo discounts."
    )
    is_hidden: bool


class DailySalesOut(BaseModel):
    day: date
    orders: int
    tickets_sold: int
    sales_kzt: int = Field(description="Order totals after promo discounts, for revenue charts.")


class CampaignStatsOut(BaseModel):
    """What one promo campaign did on confirmed orders (campaigns with no use show zeros)."""

    campaign_id: uuid.UUID
    name: str
    discount_type: DiscountType
    discount_value: int
    redemptions: int
    orders: int
    tickets_sold: int
    gross_kzt: int = Field(description="Sum of order total - discount of those orders.")
    discount_total_kzt: int
    net_kzt: int = Field(description="gross_kzt minus succeeded refunds of those orders.")


class EventAnalyticsOut(BaseModel):
    event: EventStatsOut
    ticket_types: list[TicketTypeStatsOut]
    campaigns: list[CampaignStatsOut]
    daily_sales: list[DailySalesOut] = Field(
        description="Confirmed orders per day of creation, in the venue's time zone "
        "(only days in the period, if any)."
    )
    timezone: str
    fee_percent: int
    currency: Literal["KZT"] = "KZT"
    is_simulated: Literal[True] = True
