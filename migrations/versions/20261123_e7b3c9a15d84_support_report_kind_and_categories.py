"""support report kind and categories (BE-27)

supportkind: report (a report about an event, handled by platform admins only).
supportcategory: seating, check_in, technical.

Revision ID: e7b3c9a15d84
Revises: a3f1c7d95e20
Create Date: 2026-11-23 15:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7b3c9a15d84'
down_revision: str | None = 'a3f1c7d95e20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CATEGORIES = ("seating", "check_in", "technical")


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres 12+ allows ADD VALUE inside the migration's transaction as long as nothing in
    # the same transaction uses the new value. IF NOT EXISTS makes a re-run harmless.
    op.execute("ALTER TYPE supportkind ADD VALUE IF NOT EXISTS 'report'")
    for value in NEW_CATEGORIES:
        op.execute(f"ALTER TYPE supportcategory ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres cannot drop a value from an enum type, and rebuilding the types would fail on
    # any case that already uses them. The older code never writes them, so leaving the
    # values is harmless (documented no-op, same as the BE-18 migration).
