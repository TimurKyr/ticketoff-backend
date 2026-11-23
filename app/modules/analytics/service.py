"""Organizer analytics (BE-21): sales, money and check-ins of my events, read-only.

The money rules are the payment summary's (gross - fees - refunds, fee rounded half up per
event), so an event's row here always equals GET /events/{id}/payments/summary. Totals add up
the per-event fees instead of taking a percent of the total, so the rows sum to the total.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.admin.platform_settings import get_platform_settings
from app.modules.analytics import repository
from app.modules.analytics.schemas import (
    CampaignStatsOut,
    DailySalesOut,
    EventAnalyticsOut,
    EventCounts,
    EventHistoryPage,
    EventStatsOut,
    OverviewOut,
    SalesFigures,
    TicketTypeStatsOut,
)
from app.modules.events import repository as events_repository
from app.modules.events.models import Event
from app.modules.events.schemas import EventBucket
from app.modules.identity.models import OrganizerProfile
from app.modules.payments.service import fee_for


def _now() -> datetime:
    return datetime.now(UTC)


def _figures(
    *,
    orders: int,
    tickets: int,
    checked_in: int,
    gross: int,
    discounts: int,
    fees: int,
    refunds: int,
) -> SalesFigures:
    percent = round(100 * checked_in / tickets, 1) if tickets else 0.0
    return SalesFigures(
        orders=orders,
        tickets_sold=tickets,
        checked_in=checked_in,
        check_in_percent=percent,
        gross_kzt=gross,
        discounts_kzt=discounts,
        fees_kzt=fees,
        refunds_kzt=refunds,
        estimated_payout_kzt=gross - fees - refunds,
    )


def _event_stats(session: Session, events: Iterable[Event]) -> list[EventStatsOut]:
    """One row per event, from a few grouped queries for all of them (no query per event)."""
    events = list(events)
    ids = [event.id for event in events]
    sold = repository.orders_and_tickets(session, ids)
    gross = repository.gross_kzt(session, ids)
    refunds = repository.refunds_kzt(session, ids)
    scanned = repository.checked_in(session, ids)
    discounts = repository.discounts_kzt(session, ids)
    inventory = repository.tickets_available_in_total(session, ids)
    percent = get_platform_settings(session).processing_fee_percent
    rows = []
    for event in events:
        orders, tickets = sold.get(event.id, (0, 0))
        event_gross = gross.get(event.id, 0)
        figures = _figures(
            orders=orders,
            tickets=tickets,
            checked_in=scanned.get(event.id, 0),
            gross=event_gross,
            discounts=discounts.get(event.id, 0),
            fees=fee_for(event_gross, percent),
            refunds=refunds.get(event.id, 0),
        )
        total = inventory.get(event.id, 0)
        rows.append(
            EventStatsOut(
                event_id=event.id,
                title=event.title,
                status=event.status,
                start_at=event.start_at,
                end_at=event.end_at,
                capacity=event.capacity,
                tickets_total=total,
                remaining=max(0, total - tickets),
                percent_sold=round(100 * tickets / total, 1) if total else 0.0,
                **figures.model_dump(),
            )
        )
    return rows


def _sum(rows: list[EventStatsOut]) -> SalesFigures:
    return _figures(
        orders=sum(r.orders for r in rows),
        tickets=sum(r.tickets_sold for r in rows),
        checked_in=sum(r.checked_in for r in rows),
        gross=sum(r.gross_kzt for r in rows),
        discounts=sum(r.discounts_kzt for r in rows),
        fees=sum(r.fees_kzt for r in rows),
        refunds=sum(r.refunds_kzt for r in rows),
    )


def _all_events(session: Session, organizer: OrganizerProfile) -> list[Event]:
    events, _ = events_repository.list_for_organizer(
        session, organizer_id=organizer.id, bucket=None, now=_now(), limit=100_000, offset=0
    )
    return events


def get_overview(session: Session, organizer: OrganizerProfile) -> OverviewOut:
    events = _all_events(session, organizer)
    counts = {}
    for bucket in EventBucket:
        _, counts[bucket.value] = events_repository.list_for_organizer(
            session, organizer_id=organizer.id, bucket=bucket, now=_now(), limit=1, offset=0
        )
    return OverviewOut(
        events=EventCounts(
            total=len(events), draft=repository.draft_count(session, organizer.id), **counts
        ),
        totals=_sum(_event_stats(session, events)),
        fee_percent=get_platform_settings(session).processing_fee_percent,
    )


def list_history(
    session: Session,
    organizer: OrganizerProfile,
    *,
    bucket: EventBucket | None,
    limit: int,
    offset: int,
) -> EventHistoryPage:
    """My events, newest first, each with its final numbers."""
    events, total = events_repository.list_for_organizer(
        session, organizer_id=organizer.id, bucket=bucket, now=_now(), limit=limit, offset=offset
    )
    return EventHistoryPage(
        items=_event_stats(session, events),
        total=total,
        limit=limit,
        offset=offset,
        fee_percent=get_platform_settings(session).processing_fee_percent,
    )


def get_event_analytics(session: Session, event: Event) -> EventAnalyticsOut:
    (stats,) = _event_stats(session, [event])
    return EventAnalyticsOut(
        event=stats,
        ticket_types=[
            TicketTypeStatsOut(
                ticket_type_id=tt.id,
                name=tt.name,
                kind=tt.kind,
                price_kzt=tt.price_kzt,
                quantity_total=tt.quantity_total,
                sold=sold,
                sales_kzt=sales,
                is_hidden=tt.is_hidden,
            )
            for tt, sold, sales in repository.ticket_type_rows(session, event.id)
        ],
        campaigns=[
            CampaignStatsOut(
                campaign_id=row.campaign.id,
                name=row.campaign.name,
                discount_type=row.campaign.discount_type,
                discount_value=row.campaign.discount_value,
                redemptions=row.redemptions,
                orders=row.orders,
                tickets_sold=row.tickets,
                gross_kzt=row.gross_kzt,
                discount_total_kzt=row.discount_kzt,
                net_kzt=row.gross_kzt - row.refunds_kzt,
            )
            for row in repository.campaign_rows(session, event.id)
        ],
        daily_sales=[
            DailySalesOut(day=day, orders=orders, tickets_sold=tickets, sales_kzt=sales)
            for day, orders, tickets, sales in repository.daily_sales(session, event.id)
        ],
        timezone=_venue_timezone(session, event),
        fee_percent=get_platform_settings(session).processing_fee_percent,
    )


def _venue_timezone(session: Session, event: Event) -> str:
    venue = events_repository.get_venue(session, event.venue_id)
    return venue.timezone if venue else "UTC"
