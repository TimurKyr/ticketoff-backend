import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.events.enums import EventStatus, EventVisibility, SeatingType


class Venue(Base):
    __tablename__ = "venues"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    address = Column(String)
    city = Column(String)
    timezone = Column(String, nullable=False, default="Asia/Almaty")

    sections = relationship("VenueSection", back_populates="venue")


class VenueSection(Base):
    __tablename__ = "venue_sections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    venue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("venues.id"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    price_category = Column(String)

    venue = relationship("Venue", back_populates="sections")
    rows = relationship("Row", back_populates="section")


class Row(Base):
    __tablename__ = "rows"
    # A label ("A", "12") appears once per section. The constraint's index starts with
    # section_id, so section_id needs no separate index.
    __table_args__ = (UniqueConstraint("section_id", "label"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    section_id = Column(UUID(as_uuid=True), ForeignKey("venue_sections.id"), nullable=False)
    label = Column(String, nullable=False)

    section = relationship("VenueSection", back_populates="rows")
    seats = relationship("Seat", back_populates="row")


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (UniqueConstraint("row_id", "number"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    row_id = Column(UUID(as_uuid=True), ForeignKey("rows.id"), nullable=False)
    number = Column(String, nullable=False)
    is_accessible = Column(Boolean, nullable=False, default=False)

    row = relationship("Row", back_populates="seats")


class Event(Base):
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organizer_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizer_profiles.id"),
        nullable=False,
        index=True,
    )
    venue_id = Column(
        UUID(as_uuid=True),
        ForeignKey("venues.id"),
        nullable=False,
        index=True,
    )
    title = Column(String, nullable=False)
    description = Column(Text)
    category = Column(String)
    status = Column(enum_column(EventStatus), nullable=False, default=EventStatus.DRAFT)
    visibility = Column(
        enum_column(EventVisibility),
        nullable=False,
        default=EventVisibility.PUBLIC,
    )
    seating_type = Column(
        enum_column(SeatingType),
        nullable=False,
        default=SeatingType.GENERAL,
    )
    paid_sales_active = Column(Boolean, nullable=False, default=False)
    # When the organizer accepted the paid-sales terms (activation checklist). NULL = not yet.
    paid_terms_accepted_at = Column(DateTime(timezone=True))
    capacity = Column(Integer)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True))
    registration_opens_at = Column(DateTime(timezone=True))
    registration_closes_at = Column(DateTime(timezone=True))

    venue = relationship("Venue")
    organizer = relationship("OrganizerProfile")
