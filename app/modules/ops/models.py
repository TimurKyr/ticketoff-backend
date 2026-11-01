import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.ops.enums import (
    NotificationChannel,
    NotificationType,
    SupportCategory,
    SupportKind,
    SupportStatus,
)


class CheckInRecord(Base):
    """One scan at the entrance. A mistaken scan is reversed (is_reversed), never deleted."""

    __tablename__ = "check_in_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False, index=True)
    event_admin_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    checked_in_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    is_reversed = Column(Boolean, nullable=False, default=False)

    ticket = relationship("Ticket")
    event_admin = relationship("User")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    type = Column(enum_column(NotificationType), nullable=False)
    channel = Column(enum_column(NotificationChannel), nullable=False)
    # Type-specific data, e.g. {"order_id": "..."}.
    payload = Column(JSONB)
    # NULL means unread.
    read_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user = relationship("User")


class SupportCase(Base):
    __tablename__ = "support_cases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opened_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    # NULL until a support agent picks the case up.
    assignee_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    kind = Column(enum_column(SupportKind), nullable=False)
    category = Column(enum_column(SupportCategory), nullable=False)
    status = Column(enum_column(SupportStatus), nullable=False, default=SupportStatus.OPEN)
    # Optional links to what the case is about.
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), index=True)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("tickets.id"), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Two FKs point at users, so each relationship names the column it uses.
    opener = relationship("User", foreign_keys=[opened_by])
    assignee = relationship("User", foreign_keys=[assignee_id])
    messages = relationship(
        "SupportMessage",
        back_populates="case",
        order_by="SupportMessage.created_at",
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(
        UUID(as_uuid=True),
        ForeignKey("support_cases.id"),
        nullable=False,
        index=True,
    )
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    case = relationship("SupportCase", back_populates="messages")
    sender = relationship("User")
