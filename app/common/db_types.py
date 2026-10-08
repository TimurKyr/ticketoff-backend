import enum

from sqlalchemy import Enum


def enum_column(enum_cls: type[enum.Enum]) -> Enum:
    """Postgres ENUM column that stores each member's value ("attendee"), not its name."""
    return Enum(enum_cls, values_callable=lambda members: [m.value for m in members])
