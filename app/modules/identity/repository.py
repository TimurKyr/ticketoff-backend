"""User rows (DB access only)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.identity.models import User


def lock_user(session: Session, user_id: uuid.UUID) -> User | None:
    """SELECT ... FROM users WHERE id = :id FOR UPDATE, re-read: two resends of one user run
    one after the other, so the second sees the first one's email_verification_sent_at."""
    stmt = (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalar(stmt)
