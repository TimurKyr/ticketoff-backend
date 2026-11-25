"""payout and assignment notifications, kk language (BE-28)

Revision ID: 703f7fea8054
Revises: 9a95f0f62416
Create Date: 2026-11-24 10:39:37.928882

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '703f7fea8054'
down_revision: str | None = '9a95f0f62416'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_NOTIFICATION_TYPES = ("payout_status", "support_assignment")


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres 12+ allows ADD VALUE inside the migration's transaction as long as nothing in
    # the same transaction uses the new value. IF NOT EXISTS makes a re-run harmless.
    for value in NEW_NOTIFICATION_TYPES:
        op.execute(f"ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS '{value}'")

    # Kazakh emails: kk joins ru and en.
    op.drop_constraint(op.f('ck_users_language_supported'), 'users', type_='check')
    op.create_check_constraint(
        op.f('ck_users_language_supported'), 'users', "language IN ('ru', 'en', 'kk')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # kk users go back to Russian (the fallback language), then the old CHECK fits again.
    op.execute("UPDATE users SET language = 'ru' WHERE language = 'kk'")
    op.drop_constraint(op.f('ck_users_language_supported'), 'users', type_='check')
    op.create_check_constraint(
        op.f('ck_users_language_supported'), 'users', "language IN ('ru', 'en')"
    )
    # The notificationtype values stay: Postgres cannot drop a value from an enum type, and
    # rebuilding the type would fail on any notification row that already uses them (the
    # same choice as BE-18). They are harmless to the older code, which never writes them.
