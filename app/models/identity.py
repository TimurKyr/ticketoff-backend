import uuid
from sqlalchemy import Column, String, Boolean, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base
from .enums import SystemRole, VerificationStatus, PayoutAccountStatus, StaffRole


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    system_role = Column(
        SAEnum(SystemRole, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=SystemRole.ATTENDEE,
    )
    email_verified = Column(Boolean, default=False)

    organizer_profile = relationship(
        "OrganizerProfile", back_populates="user", uselist=False
    )


class OrganizerProfile(Base):
    __tablename__ = "organizer_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    display_name = Column(String, nullable=False)
    verification_status = Column(
        SAEnum(VerificationStatus, values_callable=lambda x: [e.value for e in x]),
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
    status = Column(
        SAEnum(PayoutAccountStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=PayoutAccountStatus.PENDING,
    )
    provider_ref = Column(String)


class StaffAssignment(Base):
    __tablename__ = "staff_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    role = Column(
        SAEnum(StaffRole, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=StaffRole.EVENT_ADMIN,
    )
