"""audit.log (BE-19): the shared helper every module uses to write the audit log."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from tests.factories import create_event, create_organizer, create_user


def test_log_adds_and_flushes_one_row(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    actor = create_user(db_session)
    entity_id = uuid.uuid4()

    entry = audit.log(
        db_session,
        actor_user_id=actor.id,
        action_type="order.refunded",
        entity_type="order",
        entity_id=entity_id,
        event_id=event.id,
        details={"description": "Refunded by the organizer"},
    )

    # Flushed: a fresh query in the same transaction sees it, with a generated id.
    stored = db_session.scalars(select(AuditLog).where(AuditLog.event_id == event.id)).one()
    assert stored is entry and entry.id is not None
    assert (stored.actor_user_id, stored.action_type, stored.entity_type, stored.entity_id) == (
        actor.id,
        "order.refunded",
        "order",
        entity_id,
    )
    assert stored.details == {"description": "Refunded by the organizer"}


def test_log_never_commits(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's commit stores the action and its audit row together."""

    def no_commit() -> None:
        raise AssertionError("audit.log must not commit")

    monkeypatch.setattr(db_session, "commit", no_commit)

    audit.log(
        db_session,
        actor_user_id=None,  # system action
        action_type="seat_hold.released",
        entity_type="seat_hold",
        entity_id=None,
        event_id=None,
    )


def test_rolled_back_action_leaves_no_audit_row(db_session: Session) -> None:
    event = create_event(db_session, create_organizer(db_session))
    savepoint = db_session.begin_nested()

    audit.log(
        db_session,
        actor_user_id=None,
        action_type="order.cancelled",
        entity_type="order",
        entity_id=uuid.uuid4(),
        event_id=event.id,
    )
    savepoint.rollback()

    assert db_session.scalars(select(AuditLog).where(AuditLog.event_id == event.id)).all() == []
