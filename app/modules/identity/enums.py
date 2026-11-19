import enum


class SystemRole(enum.StrEnum):
    ATTENDEE = "attendee"
    PLATFORM_ADMIN = "platform_admin"


class Language(enum.StrEnum):
    """Language of the user's emails (BE-18). Stored as text with a CHECK, so adding one
    (e.g. "kk") is a one-line migration."""

    RU = "ru"
    EN = "en"


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
