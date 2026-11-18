import uuid

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.orders.models import Order
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund


def get_user(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def lock_user(session: Session, user_id: uuid.UUID) -> User | None:
    """SELECT ... FROM users WHERE id = :id FOR UPDATE, so two admins cannot both log a change."""
    stmt = (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalar(stmt)


def _page[T](
    session: Session, stmt: Select[tuple[T]], limit: int, offset: int
) -> tuple[list[T], int]:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.scalars(stmt.limit(limit).offset(offset))
    return list(rows), int(total or 0)


def search_users(session: Session, q: str, limit: int, offset: int) -> tuple[list[User], int]:
    # autoescape: % and _ typed by the admin are literal, not LIKE wildcards.
    stmt = (
        select(User).where(User.email.icontains(q, autoescape=True)).order_by(User.email, User.id)
    )
    return _page(session, stmt, limit, offset)


def search_events(session: Session, q: str, limit: int, offset: int) -> tuple[list[Event], int]:
    stmt = (
        select(Event)
        .where(Event.title.icontains(q, autoescape=True))
        .order_by(Event.start_at.desc(), Event.id)
    )
    return _page(session, stmt, limit, offset)


def _payment_event_id() -> ColumnElement[uuid.UUID]:
    """An activation payment carries event_id; a ticket payment reaches it through its order
    (ck_payments_ticket_order_links_order_only)."""
    return func.coalesce(Payment.event_id, Order.event_id)


def _rows_page(session: Session, stmt: Select, limit: int, offset: int) -> tuple[list[Row], int]:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.execute(stmt.limit(limit).offset(offset)).all()
    return list(rows), int(total or 0)


def list_payments(
    session: Session,
    *,
    purpose: PaymentPurpose | None,
    status: PaymentStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[Row], int]:
    """(Payment, event_id) rows, newest first."""
    stmt = (
        select(Payment, _payment_event_id())
        .outerjoin(Order, Order.id == Payment.order_id)
        .order_by(Payment.created_at.desc(), Payment.id)
    )
    if purpose is not None:
        stmt = stmt.where(Payment.purpose == purpose)
    if status is not None:
        stmt = stmt.where(Payment.status == status)
    return _rows_page(session, stmt, limit, offset)


def list_refunds(
    session: Session, *, status: RefundStatus | None, limit: int, offset: int
) -> tuple[list[Row], int]:
    """(Refund, order_id, event_id) rows, newest first."""
    stmt = (
        select(Refund, Payment.order_id, _payment_event_id())
        .join(Payment, Payment.id == Refund.payment_id)
        .outerjoin(Order, Order.id == Payment.order_id)
        .order_by(Refund.created_at.desc(), Refund.id)
    )
    if status is not None:
        stmt = stmt.where(Refund.status == status)
    return _rows_page(session, stmt, limit, offset)
