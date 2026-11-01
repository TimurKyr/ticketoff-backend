import enum


class PaymentPurpose(enum.StrEnum):
    TICKET_ORDER = "ticket_order"
    ACTIVATION = "activation"


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RefundStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
