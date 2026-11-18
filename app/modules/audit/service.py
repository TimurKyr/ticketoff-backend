"""The one way to write the audit log (BE-19). The log is append-only: rows are never
updated or deleted (ERD decision 6), so this module only adds.

action_type is "<entity>.<past-tense verb>", e.g. "payment.order_succeeded",
"order.refunded", "ticket.check_in_reversed".
"""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.modules.audit import repository
from app.modules.audit.models import AuditLog
from app.modules.audit.schemas import TimelineItemOut, TimelinePage


def log(
    session: Session,
    *,
    actor_user_id: uuid.UUID | None,
    action_type: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    event_id: uuid.UUID | None,
    details: dict | None = None,
) -> AuditLog:
    """Append one audit row in the caller's transaction: add and flush, never commit.

    The caller's own commit stores the action and its audit row together, and a rollback
    drops both, so the log never records something that did not happen.
    actor_user_id is None for system actions; a human-readable text, when useful, goes in
    details["description"].
    """
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        event_id=event_id,
        details=details,
    )
    repository.add(session, entry)
    return entry


def get_timeline(
    session: Session,
    event_id: uuid.UUID,
    *,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    action_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> TimelinePage:
    """The event's audit log, oldest first (ownership is checked by the route).

    from_date is inclusive and to_date exclusive, on created_at. action_type matches
    exactly, or as a prefix when it ends with "." (e.g. "order.").
    """
    rows, total = repository.timeline(
        session,
        event_id,
        from_date=from_date,
        to_date=to_date,
        action_type=action_type,
        limit=limit,
        offset=offset,
    )
    return TimelinePage(
        items=[TimelineItemOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
