import enum


class NotificationChannel(enum.StrEnum):
    EMAIL = "email"
    IN_APP = "in_app"


# TODO(team): the ERD lists no notification types; these are a first guess to confirm.
class NotificationType(enum.StrEnum):
    ORDER_CONFIRMED = "order_confirmed"
    TICKET_ISSUED = "ticket_issued"
    EVENT_UPDATED = "event_updated"
    EVENT_CANCELLED = "event_cancelled"
    REFUND_PROCESSED = "refund_processed"
    SUPPORT_REPLY = "support_reply"
    # BE-18. Native Postgres enum: a new value needs ALTER TYPE ... ADD VALUE in a migration.
    ACCOUNT_VERIFICATION = "account_verification"
    PAYMENT_FAILED = "payment_failed"
    SUPPORT_STATUS = "support_status"
    # BE-25 (same rule: ALTER TYPE in the migration).
    PASSWORD_RESET = "password_reset"
    # BE-28 (same rule).
    PAYOUT_STATUS = "payout_status"
    SUPPORT_ASSIGNMENT = "support_assignment"


class SupportKind(enum.StrEnum):
    ATTENDEE = "attendee"
    ORGANIZER = "organizer"
    # BE-27 (R-124): a report about an event, handled only by platform admins.
    # Native Postgres enum: a new value needs ALTER TYPE ... ADD VALUE in a migration.
    REPORT = "report"


# The categories of the spec (R-87, 4.12). `ticket` is ticket delivery, `event` is event
# information. seating, check_in and technical are BE-27 (ALTER TYPE in a migration).
class SupportCategory(enum.StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    TICKET = "ticket"
    EVENT = "event"
    ACCOUNT = "account"
    OTHER = "other"
    SEATING = "seating"
    CHECK_IN = "check_in"
    TECHNICAL = "technical"


class SupportStatus(enum.StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
