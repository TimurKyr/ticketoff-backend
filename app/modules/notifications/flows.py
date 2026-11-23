"""What each flow tells the buyer (BE-18): one function per wiring point.

Each builds the template context from the loaded rows and calls notify(), inside the
caller's transaction: the in-app row commits with the action and the email goes out after
the commit (nothing on a rollback). Callers call these right before their commit, after
every check has passed.

Recipients are always buyers (Order.buyer_user_id). Attendees named on an order are never
emailed: they did not sign up, and their address was typed by someone else.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.notifications import links, repository
from app.modules.notifications.attachments import ticket_files
from app.modules.notifications.service import notify
from app.modules.ops.enums import NotificationType
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, Ticket
from app.modules.orders.repository import checkout_hold
from app.modules.orders.ticket_data import ticket_pdf_data, venue_line
from app.modules.payments.models import Payment, Refund


def _event_context(event: Event) -> dict:
    return {
        "event_title": event.title,
        "starts_at": event.start_at,
        "tz_name": event.venue.timezone,
    }


def _order_tickets(order: Order) -> list[Ticket]:
    # Same order as the order API (order_out): by ticket id.
    return sorted((t for item in order.items for t in item.tickets), key=lambda t: t.id)


# --- orders and payments ---


def order_confirmed(session: Session, order: Order) -> None:
    """A free registration or a paid order is confirmed: the tickets, as PDFs (up to 10)."""
    session.flush()  # new tickets get their rows, so their relationships load
    event = order.event
    tickets = _order_tickets(order)
    files = ticket_files([ticket_pdf_data(t, event) for t in tickets])
    context = {
        **_event_context(event),
        "venue_line": venue_line(event.venue),
        "tickets": [
            {"ticket_type": t.order_item.ticket_type.name, "guest_name": t.attendee.name}
            for t in tickets
        ],
        "is_paid": order.status == OrderStatus.PAID,
        "total_kzt": order.total_kzt,
        "discount_kzt": order.discount_kzt,
        "paid_kzt": order.total_kzt - order.discount_kzt,
        "order_url": links.order_url(order.id),
        "tickets_url": files.tickets_url,
        "calendar_url": links.calendar_ics_url(event.id),
    }
    notify(
        session,
        order.buyer,
        NotificationType.ORDER_CONFIRMED,
        context,
        payload={"order_id": str(order.id)},
        attachments=files.attachments,
    )


def payment_failed(session: Session, order: Order, payment: Payment) -> None:
    """A simulated payment failed: the order stays pending and can be retried until expiry."""
    context = {
        **_event_context(order.event),
        "amount_kzt": payment.amount_kzt,
        "retry_until": order.created_at + checkout_hold(session),
        "order_url": links.order_url(order.id),
    }
    notify(
        session,
        order.buyer,
        NotificationType.PAYMENT_FAILED,
        context,
        payload={"order_id": str(order.id), "payment_id": str(payment.id)},
    )


def refund_processed(session: Session, order: Order, refund: Refund) -> None:
    """The organizer refunded one paid order."""
    context = {
        **_event_context(order.event),
        "amount_kzt": refund.amount_kzt,
        "reason": refund.reason,
        "order_url": links.order_url(order.id),
    }
    notify(
        session,
        order.buyer,
        NotificationType.REFUND_PROCESSED,
        context,
        # Same payload as before BE-18: the web app already reads it.
        payload={
            "order_id": str(order.id),
            "refund_id": str(refund.id),
            "amount_kzt": refund.amount_kzt,
        },
    )


# --- events ---


def event_cancelled(
    session: Session,
    event: Event,
    changed_orders: Sequence[Order],
    refunded_kzt: Mapping[uuid.UUID, int],
) -> None:
    """The event was cancelled: one email per buyer whose orders the cascade changed.

    `changed_orders`: the orders the cascade cancelled or refunded (not those that were
    already cancelled or refunded). `refunded_kzt`: order id -> amount refunded now.
    """
    by_buyer: dict[uuid.UUID, list[Order]] = {}
    for order in changed_orders:  # dict keeps the first-seen order: stable, by order id
        by_buyer.setdefault(order.buyer_user_id, []).append(order)
    # All buyers in one query: order.buyer would lazy-load one user per buyer.
    buyers = repository.users_by_id(session, by_buyer)
    # The .ics with STATUS:CANCELLED: importing it drops the entry the buyer added.
    base = {**_event_context(event), "calendar_url": links.calendar_ics_url(event.id)}
    for buyer_id, orders in by_buyer.items():
        refund = sum(refunded_kzt.get(o.id, 0) for o in orders)
        notify(
            session,
            buyers[buyer_id],
            NotificationType.EVENT_CANCELLED,
            {**base, "refund_kzt": refund, "order_urls": [links.order_url(o.id) for o in orders]},
            payload={
                "event_id": str(event.id),
                "order_ids": [str(o.id) for o in orders],
                "refund_kzt": refund,
            },
        )


@dataclass(frozen=True)
class Schedule:
    """What event_updated compares: when and where."""

    start_at: datetime
    end_at: datetime | None
    venue_id: uuid.UUID


def schedule_of(event: Event) -> Schedule:
    return Schedule(start_at=event.start_at, end_at=event.end_at, venue_id=event.venue_id)


def event_updated(session: Session, event: Event, before: Schedule) -> None:
    """The start, end or venue of a published event changed: tell every buyer holding a paid
    or free_confirmed order, once each, with old -> new values.

    Nothing for drafts (nobody holds tickets), other statuses, or other fields. Call it
    after the new values are set on `event`, with schedule_of(event) taken before.
    """
    if event.status != EventStatus.PUBLISHED:
        return
    after = schedule_of(event)
    changes: list[dict] = []
    if after.start_at != before.start_at:
        changes.append({"field": "starts_at", "old": before.start_at, "new": after.start_at})
    if after.end_at != before.end_at:
        changes.append({"field": "ends_at", "old": before.end_at, "new": after.end_at})
    if after.venue_id != before.venue_id:
        old_venue, new_venue = (
            session.get(Venue, before.venue_id),
            session.get(Venue, after.venue_id),
        )
        changes.append(
            {"field": "venue", "old": venue_line(old_venue), "new": venue_line(new_venue)}
        )
    if not changes:
        return
    # The new venue's time zone: the times are shown where the event now happens.
    context = {
        "event_title": event.title,
        "tz_name": session.get(Venue, after.venue_id).timezone,
        "changes": changes,
    }
    for buyer in repository.buyers_with_confirmed_orders(session, event.id):
        notify(
            session,
            buyer,
            NotificationType.EVENT_UPDATED,
            context,
            payload={"event_id": str(event.id), "fields": [c["field"] for c in changes]},
        )
