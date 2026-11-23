"""password reset (BE-25)

notificationtype: password_reset.
users.password_reset_sent_at: when the last reset email was queued (the per-user cooldown).

Revision ID: a3f1c7d95e20
Revises: 62da00f81c14
Create Date: 2026-11-22 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c7d95e20'
down_revision: str | None = '62da00f81c14'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'password_reset'")
    op.add_column(
        'users',
        sa.Column('password_reset_sent_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'password_reset_sent_at')
    # The notificationtype value stays: Postgres cannot drop a value from an enum type
    # (documented no-op, same as the BE-18 migration).
