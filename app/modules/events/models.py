import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
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
    capacity = Column(Integer)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True))
    registration_opens_at = Column(DateTime(timezone=True))
    registration_closes_at = Column(DateTime(timezone=True))

    venue = relationship("Venue")
    organizer = relationship("OrganizerProfile")
