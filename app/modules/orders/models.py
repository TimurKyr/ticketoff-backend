import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.orders.enums import OrderStatus, TicketKind, TicketStatus


class TicketType(Base):
    __tablename__ = "ticket_types"
    # Names expand to ck_ticket_types_<name> via the naming convention.
    __table_args__ = (
        CheckConstraint(
            "(kind = 'free' AND price_kzt = 0) OR (kind = 'paid' AND price_kzt > 0)",
            name="price_matches_kind",
        ),
        CheckConstraint("quantity_total > 0", name="quantity_total_positive"),
        CheckConstraint(
            "max_per_order IS NULL OR max_per_order >= 1",
            name="max_per_order_positive",
        ),
        CheckConstraint(
            "max_per_order IS NULL OR max_per_order <= quantity_total",
            name="max_per_order_within_total",
        ),
        CheckConstraint(
            "sales_start_at IS NULL OR sales_end_at IS NULL OR sales_start_at < sales_end_at",
            name="sales_window_valid",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("events.id"),
        nullable=False,
        index=True,
    )
    # TODO(seating): add section_id = ForeignKey("venue_sections.id") once VenueSection exists.
    name = Column(String, nullable=False)
    description = Column(Text)
    kind = Column(enum_column(TicketKind), nullable=False, default=TicketKind.FREE)
    price_kzt = Column(Integer, nullable=False, default=0)
    quantity_total = Column(Integer, nullable=False)
    # NULL means no per-order limit. No ORM default: it would also replace an explicit None
    # on insert. The default of 10 lives in TicketTypeCreate.
    max_per_order = Column(Integer)
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
