import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, true
from sqlalchemy.engine import Row
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.orders.models import Attendee, Order, OrderItem
from app.modules.orders.repository import confirmed_orders_clause
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign


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
    """(Payment, event_id, event title) rows, newest first."""
    event_id = _payment_event_id()
    stmt = (
        select(Payment, event_id, Event.title)
        .outerjoin(Order, Order.id == Payment.order_id)
        .outerjoin(Event, Event.id == event_id)
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
    """(Refund, order_id, event_id, event title) rows, newest first."""
    event_id = _payment_event_id()
    stmt = (
        select(Refund, Payment.order_id, event_id, Event.title)
        .join(Payment, Payment.id == Refund.payment_id)
        .outerjoin(Order, Order.id == Payment.order_id)
        .outerjoin(Event, Event.id == event_id)
        .order_by(Refund.created_at.desc(), Refund.id)
    )
    if status is not None:
        stmt = stmt.where(Refund.status == status)
    return _rows_page(session, stmt, limit, offset)


# --- orders (BE-26) -----------------------------------------------------------------------


def search_orders(session: Session, q: str, limit: int, offset: int) -> tuple[list[Row], int]:
    """(Order, buyer email, sales_suspended of its event) rows, newest first.

    q is an order id (exact), or part of the buyer's or of a guest's (attendee's) email.
    """
    guest = select(Attendee.order_id).where(Attendee.email.icontains(q, autoescape=True))
    match = or_(User.email.icontains(q, autoescape=True), Order.id.in_(guest))
    try:
        match = or_(match, Order.id == uuid.UUID(q))
    except ValueError:
        pass
    stmt = (
        select(Order, User.email, Event.sales_suspended)
        .join(User, User.id == Order.buyer_user_id)
        .join(Event, Event.id == Order.event_id)
        .where(match)
        .order_by(Order.created_at.desc(), Order.id)
    )
    return _rows_page(session, stmt, limit, offset)


# --- reports (BE-26). from_at inclusive, to_at exclusive, both optional. -------------------


def _period(column: InstrumentedAttribute, from_at: datetime | None, to_at: datetime | None):
    clauses = []
    if from_at is not None:
        clauses.append(column >= from_at)
    if to_at is not None:
        clauses.append(column < to_at)
    return and_(true(), *clauses)


def payments_report(
    session: Session, from_at: datetime | None, to_at: datetime | None
) -> list[Row]:
    """(Payment, event_id, event title, buyer email) rows, oldest first."""
    event_id = _payment_event_id()
    stmt = (
        select(Payment, event_id, Event.title, User.email)
        .outerjoin(Order, Order.id == Payment.order_id)
        .outerjoin(Event, Event.id == event_id)
        .outerjoin(User, User.id == Order.buyer_user_id)
        .where(_period(Payment.created_at, from_at, to_at))
        .order_by(Payment.created_at, Payment.id)
    )
    return list(session.execute(stmt).all())


def refunds_report(session: Session, from_at: datetime | None, to_at: datetime | None) -> list[Row]:
    """(Refund, Payment, event_id, buyer email) rows, oldest first."""
    stmt = (
        select(Refund, Payment, _payment_event_id(), User.email)
        .join(Payment, Payment.id == Refund.payment_id)
        .outerjoin(Order, Order.id == Payment.order_id)
        .outerjoin(User, User.id == Order.buyer_user_id)
        .where(_period(Refund.created_at, from_at, to_at))
        .order_by(Refund.created_at, Refund.id)
    )
    return list(session.execute(stmt).all())


def orders_report(session: Session, from_at: datetime | None, to_at: datetime | None) -> list[Row]:
    """(Order, event title, buyer email, tickets, promo code) rows, oldest first."""
    tickets = (
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .where(OrderItem.order_id == Order.id)
        .scalar_subquery()
    )
    code = (
        select(PromoCode.code)
        .join(PromoRedemption, PromoRedemption.promo_code_id == PromoCode.id)
        .where(PromoRedemption.order_id == Order.id)
        .scalar_subquery()
    )
    stmt = (
        select(Order, Event.title, User.email, tickets, code)
        .join(Event, Event.id == Order.event_id)
        .join(User, User.id == Order.buyer_user_id)
        .where(_period(Order.created_at, from_at, to_at))
        .order_by(Order.created_at, Order.id)
    )
    return list(session.execute(stmt).all())


def promo_activity(
    session: Session, from_at: datetime | None, to_at: datetime | None, limit: int, offset: int
) -> tuple[list[Row], int, int, int]:
    """Per campaign with a redemption in the period on a confirmed order (paid or free; the
    same rule as the organizer's analytics): (campaign, event title, codes, redemptions,
    discount, gross) rows, biggest discount first, plus the total count, the redemptions and
    the discount over every page.

    gross is what those orders were paid: total_kzt - discount_kzt.
    """
    used = (
        select(
            PromoCode.campaign_id,
            func.count().label("redemptions"),
            func.sum(PromoRedemption.discount_kzt).label("discount_kzt"),
            func.sum(Order.total_kzt - Order.discount_kzt).label("gross_kzt"),
        )
        .select_from(PromoRedemption)
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .join(Order, Order.id == PromoRedemption.order_id)
        .where(confirmed_orders_clause(), _period(PromoRedemption.created_at, from_at, to_at))
        .group_by(PromoCode.campaign_id)
        .subquery()
    )
    codes = (
        select(func.count())
        .where(PromoCode.campaign_id == PromotionalCampaign.id)
        .scalar_subquery()
    )
    stmt = (
        select(
            PromotionalCampaign,
            Event.title,
            codes,
            used.c.redemptions,
            used.c.discount_kzt,
            used.c.gross_kzt,
        )
        .join(used, used.c.campaign_id == PromotionalCampaign.id)
        .join(Event, Event.id == PromotionalCampaign.event_id)
        .order_by(used.c.discount_kzt.desc(), PromotionalCampaign.id)
    )
    rows, total = _rows_page(session, stmt, limit, offset)
    redemptions, discount = session.execute(
        select(
            func.coalesce(func.sum(used.c.redemptions), 0),
            func.coalesce(func.sum(used.c.discount_kzt), 0),
        )
    ).one()
    return rows, total, int(redemptions), int(discount)
