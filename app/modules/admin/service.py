"""Platform moderation (BE-22): suspend event sales and user accounts, search, read-only
lists of payments, refunds and activations, read-only settings.

Every suspend/unsuspend that changes something writes an audit row in the same
transaction. Repeating an action (suspend a suspended event) changes nothing and logs
nothing.
"""

import uuid

from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.config import get_settings
from app.modules.admin import repository
from app.modules.admin.schemas import (
    AdminPaymentOut,
    AdminRefundOut,
    PaymentPage,
    PlatformSettingsOut,
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


def get_settings_out() -> PlatformSettingsOut:
    settings = get_settings()
    return PlatformSettingsOut(
        activation_fee_kzt=settings.activation_fee_kzt,
        processing_fee_percent=settings.processing_fee_percent,
        checkout_hold_minutes=settings.checkout_hold_minutes,
    )
