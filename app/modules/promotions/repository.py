import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign


def add(session: Session, row: PromoCode | PromotionalCampaign | PromoRedemption) -> None:
    session.add(row)
    session.flush()


def get_campaign(
    session: Session, event_id: uuid.UUID, campaign_id: uuid.UUID
) -> PromotionalCampaign | None:
    return session.scalar(
        select(PromotionalCampaign).where(
            PromotionalCampaign.id == campaign_id, PromotionalCampaign.event_id == event_id
        )
    )


def list_campaigns(session: Session, event_id: uuid.UUID) -> list[PromotionalCampaign]:
    stmt = (
        select(PromotionalCampaign)
        .where(PromotionalCampaign.event_id == event_id)
        .order_by(PromotionalCampaign.name, PromotionalCampaign.id)
    )
    return list(session.scalars(stmt))


def get_code(session: Session, campaign_id: uuid.UUID, code_id: uuid.UUID) -> PromoCode | None:
    return session.scalar(
        select(PromoCode).where(PromoCode.id == code_id, PromoCode.campaign_id == campaign_id)
    )


def list_codes(session: Session, campaign_id: uuid.UUID) -> list[PromoCode]:
    stmt = select(PromoCode).where(PromoCode.campaign_id == campaign_id).order_by(PromoCode.code)
    return list(session.scalars(stmt))


def code_exists(session: Session, code: str) -> bool:
    return session.scalar(select(PromoCode.id).where(PromoCode.code == code)) is not None


def find_code(session: Session, code: str) -> tuple[PromoCode, PromotionalCampaign] | None:
    row = session.execute(
        select(PromoCode, PromotionalCampaign)
        .join(PromotionalCampaign, PromotionalCampaign.id == PromoCode.campaign_id)
        .where(PromoCode.code == code)
    ).one_or_none()
    return (row[0], row[1]) if row else None


def find_code_by_qr(
    session: Session, qr_token: str
) -> tuple[PromoCode, PromotionalCampaign] | None:
    row = session.execute(
        select(PromoCode, PromotionalCampaign)
        .join(PromotionalCampaign, PromotionalCampaign.id == PromoCode.campaign_id)
        .where(PromoCode.qr_token == qr_token)
    ).one_or_none()
    return (row[0], row[1]) if row else None


def _holding_redemptions(now: datetime):
    """Redemptions on orders that still count: paid, free, or pending and not yet expired.

    A cancelled or refunded order, and an abandoned checkout past its hold, give the use back
    on their own, the same way tickets are released, so no cleanup job is needed.
    """
    return orders_repository.holding_orders_clause(now, orders_repository.checkout_hold())


def campaign_redemptions(session: Session, campaign_id: uuid.UUID, now: datetime) -> int:
    stmt = (
        select(func.count())
        .select_from(PromoRedemption)
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .join(Order, Order.id == PromoRedemption.order_id)
        .where(PromoCode.campaign_id == campaign_id, _holding_redemptions(now))
    )
    return session.scalar(stmt) or 0


def code_redemptions(
    session: Session, campaign_id: uuid.UUID, now: datetime
) -> dict[uuid.UUID, int]:
    """Uses per code of one campaign (codes with no use are missing from the result)."""
    stmt = (
        select(PromoRedemption.promo_code_id, func.count())
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .join(Order, Order.id == PromoRedemption.order_id)
        .where(PromoCode.campaign_id == campaign_id, _holding_redemptions(now))
        .group_by(PromoRedemption.promo_code_id)
    )
    return {code_id: count for code_id, count in session.execute(stmt)}
