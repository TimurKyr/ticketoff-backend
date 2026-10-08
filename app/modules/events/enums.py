import enum


class EventStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class EventVisibility(enum.StrEnum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"


class SeatingType(enum.StrEnum):
    GENERAL = "general"
    ASSIGNED = "assigned"
