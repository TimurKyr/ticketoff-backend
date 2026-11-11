import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus


class Payment(Base):
    """Simulated money movement. purpose=ticket_order points at an Order,
    purpose=activation at the Event whose paid sales it activates (ERD decision 2)."""

    __tablename__ = "payments"
    # Names expand to ck_payments_<name> via the naming convention.
    __table_args__ = (
        # Exactly one link, matching the purpose. A ticket payment's event is reachable
        # through its order, so it must not also carry event_id.
        CheckConstraint(
            "purpose <> 'ticket_order' OR (order_id IS NOT NULL AND event_id IS NULL)",
            name="ticket_order_links_order_only",
        ),
        CheckConstraint(
            "purpose <> 'activation' OR (event_id IS NOT NULL AND order_id IS NULL)",
            name="activation_links_event_only",
        ),
        CheckConstraint("amount_kzt > 0", name="amount_positive"),
        # At most one succeeded payment per order and per event activation, so a double
        # pay is refused by the DB even if the row lock in the service were bypassed.
        Index(
            "uq_payments_order_succeeded",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'succeeded' AND purpose = 'ticket_order'"),
        ),
        Index(
            "uq_payments_activation_succeeded",
            "event_id",
            unique=True,
            postgresql_where=text("status = 'succeeded' AND purpose = 'activation'"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), index=True)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), index=True)
    purpose = Column(enum_column(PaymentPurpose), nullable=False)
    status = Column(enum_column(PaymentStatus), nullable=False, default=PaymentStatus.PENDING)
    # Always true in the MVP: demo payments must never look like real transactions.
    is_simulated = Column(Boolean, nullable=False, default=True)
    amount_kzt = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    order = relationship("Order")
    event = relationship("Event")
    refunds = relationship("Refund", back_populates="payment")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount_kzt > 0", name="amount_positive"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False, index=True)
    amount_kzt = Column(Integer, nullable=False)
    status = Column(enum_column(RefundStatus), nullable=False, default=RefundStatus.PENDING)
    reason = Column(String)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    payment = relationship("Payment", back_populates="refunds")
