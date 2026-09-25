import uuid
from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Boolean,
    ForeignKey,
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base
from .enums import OrderStatus, TicketStatus


class TicketType(Base):
    __tablename__ = "ticket_types"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    sales_start_at = Column(DateTime(timezone=True))
    sales_end_at = Column(DateTime(timezone=True))

    name = Column(String, nullable=False)
    description = Column(Text)
    price_kzt = Column(Integer, nullable=False, default=0)
    quantity_total = Column(Integer, nullable=False)
    max_per_order = Column(Integer, default=10)
    is_hidden = Column(Boolean, default=False)

    event = relationship("Event")


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    status = Column(
        SAEnum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    total_amount_kzt = Column(Integer, nullable=False, default=0)

    buyer = relationship("User")
    event = relationship("Event")
    items = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    ticket_type_id = Column(
        UUID(as_uuid=True), ForeignKey("ticket_types.id"), nullable=False, index=True
    )
    quantity = Column(Integer, nullable=False)
    unit_price_kzt = Column(Integer, nullable=False)

    order = relationship("Order", back_populates="items")
    ticket_type = relationship("TicketType")


class Attendee(Base):
    __tablename__ = "attendees"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    email = Column(String)


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_item_id = Column(
        UUID(as_uuid=True), ForeignKey("order_items.id"), nullable=False, index=True
    )
    attendee_id = Column(
        UUID(as_uuid=True), ForeignKey("attendees.id"), nullable=False, index=True
    )
    status = Column(
        SAEnum(TicketStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TicketStatus.VALID,
    )
    seat_id = Column(
        UUID(as_uuid=True), nullable=True
    )  # SEATING TABLE DOES NOT EXIST YET, ONCE CREATED, ADJUST THIS

    qr_token = Column(String, nullable=False, unique=True)

    order_item = relationship("OrderItem")
    attendee = relationship("Attendee")
