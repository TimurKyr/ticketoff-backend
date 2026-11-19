"""Notification rows (DB access only)."""

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.identity.models import User
from app.modules.ops.enums import NotificationChannel, NotificationType
from app.modules.ops.models import Notification
from app.modules.orders.models import Order
from app.modules.orders.repository import confirmed_orders_clause


def add_in_app(
    session: Session,
    user_id: uuid.UUID,
    type_: NotificationType,
    payload: dict | None,
) -> Notification:
    notification = Notification(
        user_id=user_id, type=type_, channel=NotificationChannel.IN_APP, payload=payload
    )
    # No flush here: the rows go out with the caller's next flush, so a flow that notifies
    # many users (event cancel) writes them in one batched INSERT, not one per user.
    session.add(notification)
    return notification


def users_by_id(session: Session, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, User]:
    """The given users in one query (no lazy load per user)."""
    ids = set(user_ids)
    if not ids:
        return {}
    return {user.id: user for user in session.scalars(select(User).where(User.id.in_(ids)))}


def buyers_with_confirmed_orders(session: Session, event_id: uuid.UUID) -> list[User]:
    """Each user holding a paid or free_confirmed order for the event, once, in id order.

    Buyers only: attendees named on an order are never emailed.
    """
    has_order = (
        select(Order.id)
        .where(
            Order.event_id == event_id, Order.buyer_user_id == User.id, confirmed_orders_clause()
        )
        .exists()
    )
    return list(session.scalars(select(User).where(has_order).order_by(User.id)))


# --- in-app list (the logged-in user's own rows only) ---


def _mine(user_id: uuid.UUID, unread_only: bool = False) -> list:
    where = [Notification.user_id == user_id, Notification.channel == NotificationChannel.IN_APP]
    if unread_only:
        where.append(Notification.read_at.is_(None))
    return where


def page_for_user(
    session: Session, user_id: uuid.UUID, *, unread_only: bool, limit: int, offset: int
) -> tuple[list[Notification], int]:
    """(one page of the user's rows, newest first; total matching rows)."""
    where = _mine(user_id, unread_only)
    total = session.scalar(select(func.count()).select_from(Notification).where(*where))
    rows = session.scalars(
        select(Notification)
        .where(*where)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)


def unread_count(session: Session, user_id: uuid.UUID) -> int:
    where = _mine(user_id, unread_only=True)
    return int(session.scalar(select(func.count()).select_from(Notification).where(*where)) or 0)


def mark_read(session: Session, user_id: uuid.UUID, notification_id: uuid.UUID) -> None:
    """Set read_at once, in one statement: a second (or concurrent) read changes nothing."""
    session.execute(
        update(Notification)
        .where(*_mine(user_id, unread_only=True), Notification.id == notification_id)
        .values(read_at=func.now())
    )


def get_for_user(
    session: Session, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification | None:
    return session.scalar(
        select(Notification)
        .where(*_mine(user_id), Notification.id == notification_id)
        .execution_options(populate_existing=True)
    )
