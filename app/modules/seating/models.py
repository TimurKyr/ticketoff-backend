import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.seating.enums import SeatHoldStatus


class SeatHold(Base):
    """Reservation of one seat: held -> converted (sold) | released (timeout/cancel).

    A refund must move converted -> released, otherwise the seat stays blocked.
    """

    __tablename__ = "seat_holds"
    __table_args__ = (
        # At most one active hold per event+seat, enforced by the database. "Active" means
        # 'held' (in checkout) or 'converted' (sold), so two buyers racing for a seat cannot
        # both win, and a sold seat cannot be held again. Only 'released' rows are free.
        # A refund must set the converted hold to 'released' to put the seat back on sale.
        Index(
            "uq_seat_holds_active_seat",
            "event_id",
            "seat_id",
            unique=True,
            postgresql_where=text("status IN ('held', 'converted')"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seat_id = Column(UUID(as_uuid=True), ForeignKey("seats.id"), nullable=False, index=True)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), index=True)
    held_until = Column(DateTime(timezone=True), nullable=False)
    status = Column(enum_column(SeatHoldStatus), nullable=False, default=SeatHoldStatus.HELD)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    seat = relationship("Seat")
    event = relationship("Event")
    order = relationship("Order")
