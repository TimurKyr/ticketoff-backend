import enum


class SeatHoldStatus(enum.StrEnum):
    HELD = "held"
    RELEASED = "released"
    CONVERTED = "converted"
