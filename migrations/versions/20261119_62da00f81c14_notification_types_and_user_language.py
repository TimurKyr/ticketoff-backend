"""notification types and user language (BE-18)

notificationtype: account_verification, payment_failed, support_status.
users.language: language of the user's emails, 'ru' (default) or 'en'.

Revision ID: 62da00f81c14
Revises: b7d2e4a91c3f
Create Date: 2026-11-19 15:04:49.904703

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '62da00f81c14'
down_revision: str | None = 'b7d2e4a91c3f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_NOTIFICATION_TYPES = ("account_verification", "payment_failed", "support_status")


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres 12+ allows ADD VALUE inside the migration's transaction as long as nothing in
    # the same transaction uses the new value. IF NOT EXISTS makes a re-run harmless.
    for value in NEW_NOTIFICATION_TYPES:
        op.execute(f"ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column(
        'users',
        sa.Column('language', sa.String(length=2), server_default=sa.text("'ru'"), nullable=False),
    )
    op.create_check_constraint(op.f('ck_users_language_supported'), 'users', "language IN ('ru', 'en')")
    # The resend cooldown of POST /auth/verify-email/resend counts from it (NULL: never sent).
    op.add_column(
        'users',
        sa.Column('email_verification_sent_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'email_verification_sent_at')
    op.drop_constraint(op.f('ck_users_language_supported'), 'users', type_='check')
    op.drop_column('users', 'language')
    # The notificationtype values stay: Postgres cannot drop a value from an enum type, and
    # rebuilding the type would fail on any notification row that already uses them. They are
    # unused by the older code, so leaving them is harmless (documented no-op).
