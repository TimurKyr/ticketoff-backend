import enum


class DiscountType(enum.StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"


class PromoCodeStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"
