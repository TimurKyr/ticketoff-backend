"""Platform moderation (BE-22): suspend event sales and user accounts, search, read-only
lists of payments, refunds and activations. BE-26: order search, CSV reports, promo activity
and editable platform settings.

Every suspend/unsuspend that changes something writes an audit row in the same
transaction. Repeating an action (suspend a suspended event) changes nothing and logs
nothing.
"""

import csv
import io
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, InvalidInputError, NotFoundError
from app.modules.admin import repository
from app.modules.admin.models import PlatformSettings
from app.modules.admin.platform_settings import (
    SETTINGS_ROW_ID,
    env_defaults,
    get_platform_settings,
)
from app.modules.admin.schemas import (
    AdminPaymentOut,
    AdminRefundOut,
    PaymentPage,
    PlatformSettingsOut,
    PlatformSettingsUpdate,
    PromoActivityOut,
    PromoActivityPage,
    RefundPage,
    SearchItemOut,
    SearchPage,
    SearchType,
)
from app.modules.audit import service as audit
from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.orders import repository as orders_repository
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus


def set_event_sales_suspended(
    session: Session, admin: User, event_id: uuid.UUID, suspended: bool
) -> Event:
    """Stop (or restore) new sales of any event. Tickets already issued stay valid.

    Takes the event lock, the first lock of every sale (orders/repository.py lock order):
    a registration, checkout or pay in progress finishes first; any that comes after sees
    the new value on its locked row.
    """
    event = orders_repository.lock_event(session, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    if event.sales_suspended != suspended:
        event.sales_suspended = suspended
        audit.log(
            session,
            actor_user_id=admin.id,
            action_type="event.sales_suspended" if suspended else "event.sales_unsuspended",
            entity_type="event",
            entity_id=event.id,
            event_id=event.id,
            details={"by": "platform_admin"},
        )
    session.commit()
    return event


def set_user_suspended(session: Session, admin: User, user_id: uuid.UUID, suspended: bool) -> User:
    """Block (or unblock) an account: login and every existing token answer 403."""
    if user_id == admin.id:
        raise BusinessRuleError("You cannot suspend or unsuspend your own account")
    user = repository.lock_user(session, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if user.is_suspended != suspended:
        user.is_suspended = suspended
        audit.log(
            session,
            actor_user_id=admin.id,
            action_type="user.suspended" if suspended else "user.unsuspended",
            entity_type="user",
            entity_id=user.id,
            event_id=None,
            details={"email": user.email},
        )
    session.commit()
    return user


def search(session: Session, q: str, type_: SearchType, limit: int, offset: int) -> SearchPage:
    q = q.strip()
    if type_ == "user":
        users, total = repository.search_users(session, q, limit, offset)
        items = [
            SearchItemOut(
                type="user",
                id=u.id,
                label=u.email,
                status=u.system_role,
                suspended=u.is_suspended,
            )
            for u in users
        ]
    elif type_ == "order":
        rows, total = repository.search_orders(session, q, limit, offset)
        items = [
            SearchItemOut(
                type="order",
                id=order.id,
                label=f"{email} · {order.total_kzt - order.discount_kzt} KZT · {order.status}",
                status=order.status,
                suspended=sales_suspended,
            )
            for order, email, sales_suspended in rows
        ]
    else:
        events, total = repository.search_events(session, q, limit, offset)
        items = [
            SearchItemOut(
                type="event",
                id=e.id,
                label=e.title,
                status=e.status,
                suspended=e.sales_suspended,
            )
            for e in events
        ]
    return SearchPage(items=items, total=total, limit=limit, offset=offset)


def list_payments(
    session: Session,
    *,
    purpose: PaymentPurpose | None,
    status: PaymentStatus | None,
    limit: int,
    offset: int,
) -> PaymentPage:
    rows, total = repository.list_payments(
        session, purpose=purpose, status=status, limit=limit, offset=offset
    )
    return PaymentPage(
        items=[
            AdminPaymentOut(
                id=payment.id,
                purpose=payment.purpose,
                status=payment.status,
                order_id=payment.order_id,
                event_id=event_id,
                amount_kzt=payment.amount_kzt,
                is_simulated=payment.is_simulated,
                created_at=payment.created_at,
            )
            for payment, event_id in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_refunds(
    session: Session, *, status: RefundStatus | None, limit: int, offset: int
) -> RefundPage:
    rows, total = repository.list_refunds(session, status=status, limit=limit, offset=offset)
    items = [
        AdminRefundOut(
            id=refund.id,
            payment_id=refund.payment_id,
            order_id=order_id,
            event_id=event_id,
            amount_kzt=refund.amount_kzt,
            status=refund.status,
            reason=refund.reason,
            created_at=refund.created_at,
        )
        for refund, order_id, event_id in rows
    ]
    return RefundPage(items=items, total=total, limit=limit, offset=offset)


def get_settings_out(session: Session) -> PlatformSettingsOut:
    row = session.get(PlatformSettings, SETTINGS_ROW_ID)
    fees = get_platform_settings(session)
    return PlatformSettingsOut(
        activation_fee_kzt=fees.activation_fee_kzt,
        processing_fee_percent=fees.processing_fee_percent,
        checkout_hold_minutes=fees.checkout_hold_minutes,
        updated_at=row.updated_at if row is not None else None,
    )


def update_settings(
    session: Session, admin: User, data: PlatformSettingsUpdate
) -> PlatformSettingsOut:
    """Save the fees sent. The first change copies the env values into the row, then
    overwrites the fields sent. Audited (admin.settings_updated) with the old and new value
    of each field that changed; sending the current values changes and logs nothing."""
    # The row lock serializes two admins; ON CONFLICT covers two first saves at once.
    defaults = env_defaults()
    session.execute(
        pg_insert(PlatformSettings)
        .values(
            id=SETTINGS_ROW_ID,
            activation_fee_kzt=defaults.activation_fee_kzt,
            processing_fee_percent=defaults.processing_fee_percent,
            checkout_hold_minutes=defaults.checkout_hold_minutes,
        )
        .on_conflict_do_nothing(index_elements=[PlatformSettings.id])
    )
    row = session.scalars(
        select(PlatformSettings)
        .where(PlatformSettings.id == SETTINGS_ROW_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    changes: dict[str, dict[str, int]] = {}
    for name, new in data.model_dump(exclude_unset=True).items():
        old = getattr(row, name)
        if old != new:
            setattr(row, name, new)
            changes[name] = {"old": old, "new": new}
    if changes:
        row.updated_by_user_id = admin.id
        audit.log(
            session,
            actor_user_id=admin.id,
            action_type="admin.settings_updated",
            entity_type="platform_settings",
            entity_id=None,
            event_id=None,
            details=changes,
        )
    session.commit()
    return get_settings_out(session)


# --- CSV reports (BE-26) ---

# Report dates are calendar days in Kazakhstan; timestamps in the file are UTC (ISO 8601).
REPORT_TZ = ZoneInfo("Asia/Almaty")
ReportKind = Literal["payments", "refunds", "orders"]


def _bounds(from_date: date | None, to_date: date | None) -> tuple[datetime | None, ...]:
    """from_date and to_date are both inclusive days in REPORT_TZ."""
    if from_date is not None and to_date is not None and from_date > to_date:
        raise InvalidInputError("from_date must not be after to_date")
    start = datetime.combine(from_date, time(), REPORT_TZ) if from_date else None
    end = datetime.combine(to_date + timedelta(days=1), time(), REPORT_TZ) if to_date else None
    return start, end


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _cell(value: object) -> str:
    """One CSV cell. Booleans as true/false, None as empty. Free text (event titles,
    refund reasons) that a spreadsheet would run as a formula gets a leading '."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return str(value)


def _report_rows(
    session: Session, kind: ReportKind, start: datetime | None, end: datetime | None
) -> tuple[list[str], list[list[object]]]:
    if kind == "payments":
        header = [
            "payment_id", "created_at", "purpose", "status", "amount_kzt", "order_id",
            "event_id", "event_title", "buyer_email", "is_simulated",
        ]  # fmt: skip
        rows = [
            [
                p.id,
                _iso(p.created_at),
                p.purpose,
                p.status,
                p.amount_kzt,
                p.order_id,
                event_id,
                title,
                email,
                p.is_simulated,
            ]  # fmt: skip
            for p, event_id, title, email in repository.payments_report(session, start, end)
        ]
    elif kind == "refunds":
        header = [
            "refund_id", "created_at", "status", "amount_kzt", "reason", "payment_id",
            "order_id", "event_id", "buyer_email", "is_simulated",
        ]  # fmt: skip
        rows = [
            [
                r.id,
                _iso(r.created_at),
                r.status,
                r.amount_kzt,
                r.reason,
                p.id,
                p.order_id,
                event_id,
                email,
                p.is_simulated,
            ]  # fmt: skip
            for r, p, event_id, email in repository.refunds_report(session, start, end)
        ]
    else:
        header = [
            "order_id", "created_at", "status", "event_id", "event_title", "buyer_email",
            "tickets", "total_kzt", "discount_kzt", "paid_kzt", "promo_code", "is_simulated",
        ]  # fmt: skip
        # Every payment is simulated (ck_payments_is_simulated), so every order is too.
        rows = [
            [
                o.id,
                _iso(o.created_at),
                o.status,
                o.event_id,
                title,
                email,
                tickets,
                o.total_kzt,
                o.discount_kzt,
                o.total_kzt - o.discount_kzt,
                code,
                True,
            ]  # fmt: skip
            for o, title, email, tickets, code in repository.orders_report(session, start, end)
        ]
    return header, rows


def build_report(
    session: Session, kind: ReportKind, from_date: date | None, to_date: date | None
) -> tuple[str, bytes]:
    """(file name, CSV bytes): UTF-8 with a BOM so Excel opens Cyrillic right, comma
    separated, one header row, then one row per record in the period, oldest first.
    No card data exists to leak; the only personal data is the buyer's email."""
    start, end = _bounds(from_date, to_date)
    header, rows = _report_rows(session, kind, start, end)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    period = f"{from_date or 'start'}_{to_date or 'now'}"
    return f"ticketoff-{kind}-{period}.csv", ("\ufeff" + out.getvalue()).encode("utf-8")


def promo_activity(
    session: Session, from_date: date | None, to_date: date | None, limit: int, offset: int
) -> PromoActivityPage:
    start, end = _bounds(from_date, to_date)
    rows, total, redemptions, discount = repository.promo_activity(
        session, start, end, limit, offset
    )
    return PromoActivityPage(
        items=[
            PromoActivityOut(
                event_id=campaign.event_id,
                event_title=title,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                codes=codes,
                redemptions=used,
                discount_total_kzt=int(discount_kzt),
                gross_kzt=int(gross),
            )
            for campaign, title, codes, used, discount_kzt, gross in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        total_redemptions=redemptions,
        total_discount_kzt=discount,
    )
