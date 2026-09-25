import uuid
from sqlalchemy import (
    Column,
    String,
    Text,
    Boolean,
    Integer,
    DateTime,
    ForeignKey,
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base
from .enums import EventStatus


class Venue(Base):
    __tablename__ = "venues"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    address = Column(String)
    city = Column(String)


class Event(Base):
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organizer_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    venue_id = Column(
        UUID(as_uuid=True), ForeignKey("venues.id"), nullable=False, index=True
    )
    status = Column(
        SAEnum(EventStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EventStatus.DRAFT,
    )
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True))
    registration_opens_at = Column(DateTime(timezone=True))
    registration_closes_at = Column(DateTime(timezone=True))
    capacity = Column(Integer)
    is_assigned_seating = Column(Boolean, default=False)
    is_paid_activated = Column(Boolean, default=False)

    venue = relationship("Venue")
    organizer = relationship("User")
