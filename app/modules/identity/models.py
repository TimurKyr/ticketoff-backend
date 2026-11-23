import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.common.base import Base
from app.common.db_types import enum_column
from app.modules.identity.enums import (
    Language,
    PayoutAccountStatus,
    StaffRole,
    SystemRole,
    VerificationStatus,
)


class User(Base):
    __tablename__ = "users"
    # Names expand to ck_users_<name> via the naming convention.
    __table_args__ = (CheckConstraint("language IN ('ru', 'en')", name="language_supported"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    system_role = Column(enum_column(SystemRole), nullable=False, default=SystemRole.ATTENDEE)
    email_verified = Column(Boolean, nullable=False, default=False)
    # Blocked by a platform admin (BE-22): login and every existing token answer 403.
    is_suspended = Column(Boolean, nullable=False, default=False, server_default=false())
    # Language of the user's emails (BE-18); set at registration.
    language = Column(String(2), nullable=False, default=Language.RU, server_default=text("'ru'"))
    # When the last verification email was queued (BE-18): the resend cooldown counts from it.
    email_verification_sent_at = Column(DateTime(timezone=True))
    # When the last password reset email was queued (BE-25): its cooldown counts from it.
    password_reset_sent_at = Column(DateTime(timezone=True))

    organizer_profile = relationship("OrganizerProfile", back_populates="user", uselist=False)


class OrganizerProfile(Base):
    __tablename__ = "organizer_profiles"
    # Formats are validated in the API (schemas.py); these keep the stored forms honest too.
    __table_args__ = (
        CheckConstraint(
            "contact_phone IS NULL OR contact_phone ~ '^\\+[0-9]{10,15}$'",
            name="contact_phone_format",
        ),
        CheckConstraint("website IS NULL OR website ~ '^https?://'", name="website_http"),
        CheckConstraint(
            "contact_email IS NULL OR contact_email = lower(contact_email)",
            name="contact_email_lowercase",
        ),
    )

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
    # Contacts shown to buyers (BE-29), all optional. contact_email is public on events;
    # contact_phone is never shown publicly. Stored forms: lower-case email, "+" and digits.
    contact_email = Column(String(254))
    contact_phone = Column(String(16))
    website = Column(String(255))

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
