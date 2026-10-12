import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.orders.enums import OrderStatus, TicketKind, TicketStatus


class TicketType(Base):
    __tablename__ = "ticket_types"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("events.id"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    description = Column(Text)
    kind = Column(enum_column(TicketKind), nullable=False, default=TicketKind.FREE)
    price_kzt = Column(Integer, nullable=False, default=0)
    quantity_total = Column(Integer, nullable=False)
    max_per_order = Column(Integer, default=10)
    sales_start_at = Column(DateTime(timezone=True))
    sales_end_at = Column(DateTime(timezone=True))
    is_hidden = Column(Boolean, nullable=False, default=False)

    event = relationship("Event")


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("events.id"),
        nullable=False,
        index=True,
    )
    status = Column(enum_column(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    total_kzt = Column(Integer, nullable=False, default=0)
    discount_kzt = Column(Integer, nullable=False, default=0)

    buyer = relationship("User")
    event = relationship("Event")
    items = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
        index=True,
    )
    ticket_type_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ticket_types.id"),
        nullable=False,
        index=True,
    )
    # TODO(seating): add ForeignKey("seats.id") once the Seat table exists.
    seat_id = Column(UUID(as_uuid=True))
    quantity = Column(Integer, nullable=False)
    unit_price_kzt = Column(Integer, nullable=False)

    order = relationship("Order", back_populates="items")
    ticket_type = relationship("TicketType")


class Attendee(Base):
    __tablename__ = "attendees"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    email = Column(String)


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("order_items.id"),
        nullable=False,
        index=True,
    )
    attendee_id = Column(
        UUID(as_uuid=True),
        ForeignKey("attendees.id"),
        nullable=False,
        index=True,
    )
    # TODO(seating): add ForeignKey("seats.id") once the Seat table exists.
    seat_id = Column(UUID(as_uuid=True))
    qr_token = Column(String, nullable=False, unique=True)
    status = Column(enum_column(TicketStatus), nullable=False, default=TicketStatus.VALID)

    order_item = relationship("OrderItem")
    attendee = relationship("Attendee")
