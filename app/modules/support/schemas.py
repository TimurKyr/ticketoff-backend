import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.ops.enums import SupportCategory, SupportKind, SupportStatus

# Spaces around the text are dropped, and a message of only spaces is refused (422).
MessageBody = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)
]


class CaseCreate(BaseModel):
    """Open a case. The first message is the case text; there is no separate title.

    `kind` says who is asking: "attendee" (a ticket buyer, answered by the event's organizer)
    or "organizer" (an organizer asking the platform, answered by a platform admin).
    An attendee case must point to an event, directly or through an order or ticket.
    """

    kind: SupportKind
    category: SupportCategory
    body: MessageBody
    event_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    ticket_id: uuid.UUID | None = None


class MessageCreate(BaseModel):
    body: MessageBody


class StatusUpdate(BaseModel):
    # "open" is only the starting status; nobody sets it by hand.
    status: Literal[
        SupportStatus.IN_PROGRESS, SupportStatus.WAITING_CUSTOMER, SupportStatus.RESOLVED
    ]


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    sender_id: uuid.UUID
    body: str
    created_at: datetime


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
