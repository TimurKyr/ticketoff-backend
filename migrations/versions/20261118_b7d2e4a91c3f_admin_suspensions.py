"""admin suspensions (BE-22)

events.sales_suspended: a platform admin stopped new sales (free registration, checkout,
pay); existing tickets stay valid.
users.is_suspended: a platform admin blocked the account; login and every token answer 403.

Revision ID: b7d2e4a91c3f
Revises: 6fe1e29bcdce
Create Date: 2026-11-18 18:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4a91c3f'
down_revision: str | None = '6fe1e29bcdce'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'events',
        sa.Column('sales_suspended', sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        'users',
        sa.Column('is_suspended', sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'is_suspended')
    op.drop_column('events', 'sales_suspended')
