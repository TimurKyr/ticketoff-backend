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


# Orders in these statuses hold inventory; cancelled and refunded ones release it.
INVENTORY_HOLDING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.PAID,
    OrderStatus.FREE_CONFIRMED,
)


class TicketStatus(enum.StrEnum):
    VALID = "valid"
    CHECKED_IN = "checked_in"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
