import uuid

from sqlalchemy import Boolean, Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.identity.enums import (
    PayoutAccountStatus,
    StaffRole,
    SystemRole,
    VerificationStatus,
)


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    system_role = Column(enum_column(SystemRole), nullable=False, default=SystemRole.ATTENDEE)
    email_verified = Column(Boolean, nullable=False, default=False)

    organizer_profile = relationship("OrganizerProfile", back_populates="user", uselist=False)


class OrganizerProfile(Base):
    __tablename__ = "organizer_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )
    display_name = Column(String, nullable=False)
    verification_status = Column(
        enum_column(VerificationStatus),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
    )

    user = relationship("User", back_populates="organizer_profile")


class PayoutAccount(Base):
    __tablename__ = "payout_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organizer_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizer_profiles.id"),
        nullable=False,
        index=True,
    )
    provider_ref = Column(String)
    status = Column(
        enum_column(PayoutAccountStatus),
        nullable=False,
        default=PayoutAccountStatus.PENDING,
    )


class StaffAssignment(Base):
    __tablename__ = "staff_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("events.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    role = Column(enum_column(StaffRole), nullable=False, default=StaffRole.EVENT_ADMIN)
