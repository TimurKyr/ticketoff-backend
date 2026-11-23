import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, computed_field, model_validator

from app.modules.ops.enums import SupportCategory, SupportKind, SupportStatus

# The categories a report about an event may use (BE-27).
REPORT_CATEGORIES = (SupportCategory.EVENT, SupportCategory.OTHER)

# Spaces around the text are dropped, and a message of only spaces is refused (422).
MessageBody = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)
]


class CaseCreate(BaseModel):
    """Open a case. The first message is the case text; there is no separate title.

    `kind` says who is asking: "attendee" (a ticket buyer, answered by the event's organizer)
    or "organizer" (an organizer asking the platform, answered by a platform admin).
    An attendee case must point to an event, directly or through an order or ticket.

    `kind` "report" (BE-27) reports a published event to the platform: it needs `event_id`
    (any published public or unlisted event, bought or not) and the category "event" or
    "other", and takes no order or ticket. Only platform admins handle reports; the event's
    organizer never sees them.
    """

    kind: SupportKind
    category: SupportCategory
    body: MessageBody
    event_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    ticket_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def check_report(self) -> Self:
        if self.kind != SupportKind.REPORT:
            return self
        if self.event_id is None:
            raise ValueError("A report needs an event_id")
        if self.order_id is not None or self.ticket_id is not None:
            raise ValueError("A report is about an event: leave out order_id and ticket_id")
        if self.category not in REPORT_CATEGORIES:
            raise ValueError("A report's category must be event or other")
        return self


class StatusUpdate(BaseModel):
    # "open" is only the starting status; nobody sets it by hand.
    status: Literal[
        SupportStatus.IN_PROGRESS, SupportStatus.WAITING_CUSTOMER, SupportStatus.RESOLVED
    ]


class AttachmentOut(BaseModel):
    """A file sent with a message (BE-33). `url` is the download path (needs the login)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def url(self) -> str:
        return f"/support/attachments/{self.id}"


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    sender_id: uuid.UUID
    body: str
    created_at: datetime
    attachments: list[AttachmentOut] = []


class CaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opened_by: uuid.UUID
    # None until someone picks the case up.
    assignee_id: uuid.UUID | None
    kind: SupportKind
    category: SupportCategory
    status: SupportStatus
    event_id: uuid.UUID | None
    order_id: uuid.UUID | None
    ticket_id: uuid.UUID | None
    created_at: datetime
    # For display (FE-8): users have no name, so the opener is shown by email. Only people
    # who can see the case (its opener or a handler) ever get it.
    opener_email: str
    # None when the case has no event (an organizer case may have none).
    event_title: str | None


class CaseDetailOut(CaseOut):
    messages: list[MessageOut]


class CasePage(BaseModel):
    """One page of cases. total counts every matching case."""

    items: list[CaseOut]
    total: int
    limit: int
    offset: int
