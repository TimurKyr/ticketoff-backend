import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.promotions.enums import DiscountType, PromoCodeStatus


class PromotionalCampaign(Base):
    __tablename__ = "promotional_campaigns"
    __table_args__ = (
        CheckConstraint("discount_value > 0", name="discount_value_positive"),
        CheckConstraint(
            "discount_type <> 'percent' OR discount_value <= 100",
            name="percent_at_most_100",
        ),
        CheckConstraint(
            "max_redemptions IS NULL OR max_redemptions >= 1",
            name="max_redemptions_positive",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    discount_type = Column(enum_column(DiscountType), nullable=False)
    # Percent (1-100) or a fixed amount in KZT, depending on discount_type.
    discount_value = Column(Integer, nullable=False)
    # NULL means unlimited.
    max_redemptions = Column(Integer)

    event = relationship("Event")
    codes = relationship("PromoCode", back_populates="campaign")


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(
        UUID(as_uuid=True),
        ForeignKey("promotional_campaigns.id"),
        nullable=False,
        index=True,
    )
    code = Column(String, nullable=False, unique=True)
    # Campaign QR (tracked link), NOT an entry ticket: the check-in scanner must reject it.
    qr_token = Column(String, nullable=False, unique=True)
    status = Column(enum_column(PromoCodeStatus), nullable=False, default=PromoCodeStatus.ACTIVE)

    campaign = relationship("PromotionalCampaign", back_populates="codes")


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"
    __table_args__ = (CheckConstraint("discount_kzt >= 0", name="discount_non_negative"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    promo_code_id = Column(
        UUID(as_uuid=True),
        ForeignKey("promo_codes.id"),
        nullable=False,
        index=True,
    )
    # unique: at most one promo code per order (ERD: ORDER ||--o| PROMO_REDEMPTION).
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, unique=True)
    discount_kzt = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    promo_code = relationship("PromoCode")
    order = relationship("Order")
