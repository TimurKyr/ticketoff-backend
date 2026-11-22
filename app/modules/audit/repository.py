import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog


def add(session: Session, entry: AuditLog) -> None:
    """Insert one audit row and flush it; the caller's commit makes it permanent."""
    session.add(entry)
    session.flush()


def has_action(session: Session, event_id: uuid.UUID, action_type: str) -> bool:
    """Whether the event's log has at least one row of this exact action type."""
    stmt = (
        select(AuditLog.id)
        .where(AuditLog.event_id == event_id, AuditLog.action_type == action_type)
        .limit(1)
    )
    return session.scalar(stmt) is not None


def _timeline_filters(
    event_id: uuid.UUID,
    from_date: datetime | None,
    to_date: datetime | None,
    action_type: str | None,
) -> list[ColumnElement[bool]]:
    filters = [AuditLog.event_id == event_id]
    if from_date is not None:
        filters.append(AuditLog.created_at >= from_date)  # inclusive
    if to_date is not None:
        filters.append(AuditLog.created_at < to_date)  # exclusive
    if action_type is not None:
        if action_type.endswith("."):
            # A prefix such as "order.". autoescape: "_" and "%" in action types (e.g.
            # "ticket.check_in_reversed") are literal, not LIKE wildcards.
            filters.append(AuditLog.action_type.startswith(action_type, autoescape=True))
        else:
            filters.append(AuditLog.action_type == action_type)
    return filters


def timeline(
    session: Session,
    event_id: uuid.UUID,
    *,
    from_date: datetime | None,
    to_date: datetime | None,
    action_type: str | None,
    limit: int,
    offset: int,
) -> tuple[list[AuditLog], int]:
    """(one page of the event's audit rows, oldest first; total matching rows).

    created_at is the DB's now(), the start of the writing transaction, so rows written
    in one transaction share it; id breaks such ties so pages are stable.
    """
    filters = _timeline_filters(event_id, from_date, to_date, action_type)
    total = session.scalar(select(func.count()).select_from(AuditLog).where(*filters))
    rows = session.scalars(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at, AuditLog.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)
