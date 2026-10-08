import enum


class TicketKind(enum.StrEnum):
    FREE = "free"
    PAID = "paid"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FREE_CONFIRMED = "free_confirmed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class TicketStatus(enum.StrEnum):
    VALID = "valid"
    CHECKED_IN = "checked_in"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
