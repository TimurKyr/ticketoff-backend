"""event updated_at (BE-31)

events.updated_at: when the row last changed. The calendar file's SEQUENCE and
LAST-MODIFIED come from it, so a calendar app replaces its copy after an edit or a cancel.
Existing rows get now().

Revision ID: c4a1e7d93b20
Revises: 62da00f81c14
Create Date: 2026-11-22 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a1e7d93b20'
down_revision: str | None = '62da00f81c14'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'events',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('events', 'updated_at')
