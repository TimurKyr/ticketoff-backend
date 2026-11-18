"""event refund policy and refund guard (BE-19)

events.refund_policy: optional text buyers read before paying.
uq_refunds_payment_succeeded: full refunds only, so at most one succeeded refund per
payment; a double refund is refused by the DB even if the service lock were bypassed.

Revision ID: 6fe1e29bcdce
Revises: e39014069e0e
Create Date: 2026-11-18 13:41:28.504019

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6fe1e29bcdce'
down_revision: str | None = 'e39014069e0e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('events', sa.Column('refund_policy', sa.Text(), nullable=True))
    op.create_index('uq_refunds_payment_succeeded', 'refunds', ['payment_id'], unique=True, postgresql_where=sa.text("status = 'succeeded'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_refunds_payment_succeeded', table_name='refunds', postgresql_where=sa.text("status = 'succeeded'"))
    op.drop_column('events', 'refund_policy')
