"""guard payments is_simulated (BE-14 review)

All payments are simulated in the MVP: the DB defaults is_simulated to true and refuses
false. A separate revision because 4901b84fc0d7 is already on main and must not change.

Revision ID: e39014069e0e
Revises: 4901b84fc0d7
Create Date: 2026-11-17 21:56:49.005775

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e39014069e0e'
down_revision: str | None = '4901b84fc0d7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('payments', 'is_simulated', existing_type=sa.Boolean(), existing_nullable=False, server_default=sa.true())
    # Fails if any existing row has is_simulated = false; none can exist (the app only writes true).
    op.create_check_constraint(op.f('ck_payments_is_simulated'), 'payments', 'is_simulated')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_payments_is_simulated'), 'payments', type_='check')
    op.alter_column('payments', 'is_simulated', existing_type=sa.Boolean(), existing_nullable=False, server_default=None)
