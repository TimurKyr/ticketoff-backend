import enum


class SystemRole(enum.StrEnum):
    ATTENDEE = "attendee"
    PLATFORM_ADMIN = "platform_admin"


class VerificationStatus(enum.StrEnum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"


class PayoutAccountStatus(enum.StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class StaffRole(enum.StrEnum):
    EVENT_ADMIN = "event_admin"
