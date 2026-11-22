import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.events.enums import EventStatus
from app.modules.orders.enums import TicketKind


class SalesFigures(BaseModel):
    """Sales numbers of one event, or the sum over several. Money is whole KZT, simulated."""

    orders: int = Field(description="Paid or free-confirmed orders.")
    tickets_sold: int = Field(description="Tickets in those orders (refunded orders excluded).")
    checked_in: int
    check_in_percent: float = Field(description="checked_in / tickets_sold, 0 when none sold.")
    gross_kzt: int
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


class EventHistoryPage(BaseModel):
    items: list[EventStatsOut]
    total: int
    limit: int
    offset: int
    fee_percent: int
    currency: Literal["KZT"] = "KZT"
    is_simulated: Literal[True] = True


class EventCounts(BaseModel):
    """How many of my events fall in each dashboard bucket (the buckets can overlap)."""

    total: int
    upcoming: int
    active: int
    completed: int
    cancelled: int


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


class EventAnalyticsOut(BaseModel):
    event: EventStatsOut
    ticket_types: list[TicketTypeStatsOut]
    daily_sales: list[DailySalesOut] = Field(
        description="Confirmed orders per day of creation, in the venue's time zone."
    )
    timezone: str
    fee_percent: int
    currency: Literal["KZT"] = "KZT"
    is_simulated: Literal[True] = True
