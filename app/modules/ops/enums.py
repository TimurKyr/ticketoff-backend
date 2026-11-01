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


class SupportKind(enum.StrEnum):
    ATTENDEE = "attendee"
    ORGANIZER = "organizer"


# TODO(team): the ERD lists no support categories; these are a first guess to confirm.
class SupportCategory(enum.StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    TICKET = "ticket"
    EVENT = "event"
    ACCOUNT = "account"
    OTHER = "other"


class SupportStatus(enum.StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
