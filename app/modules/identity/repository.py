"""User rows (DB access only)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.identity.enums import PayoutAccountStatus
from app.modules.identity.models import PayoutAccount, User


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


# Best first: a verified account is what paid sales need (payments.get_verified_payout_account).
_PAYOUT_RANK = (
    PayoutAccountStatus.VERIFIED,
    PayoutAccountStatus.PENDING,
    PayoutAccountStatus.REJECTED,
)


def payout_status(session: Session, organizer_id: uuid.UUID) -> PayoutAccountStatus | None:
    """The organizer's payout account status (the best one if there are several), or None."""
    statuses = set(
        session.scalars(
            select(PayoutAccount.status).where(PayoutAccount.organizer_id == organizer_id)
        )
    )
    return next((s for s in _PAYOUT_RANK if s in statuses), None)
