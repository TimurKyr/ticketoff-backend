from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, SmallInteger, func
from sqlalchemy.dialects.postgresql import UUID

from app.common.base import Base


class PlatformSettings(Base):
    """Platform fees a platform admin can change (BE-26). At most one row (id = 1).

    No row means nobody has changed anything yet: the values come from the env
    (app.core.config), see app.modules.admin.platform_settings.
    """

    __tablename__ = "platform_settings"
    # Names expand to ck_platform_settings_<name>; the bounds match config.py.
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("activation_fee_kzt >= 1", name="activation_fee_positive"),
        CheckConstraint(
            "processing_fee_percent BETWEEN 0 AND 100", name="processing_fee_percent_range"
        ),
        CheckConstraint("checkout_hold_minutes >= 1", name="checkout_hold_positive"),
    )

    id = Column(SmallInteger, primary_key=True, default=1, autoincrement=False)
    activation_fee_kzt = Column(Integer, nullable=False)
    processing_fee_percent = Column(Integer, nullable=False)
    checkout_hold_minutes = Column(Integer, nullable=False)
    updated_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
