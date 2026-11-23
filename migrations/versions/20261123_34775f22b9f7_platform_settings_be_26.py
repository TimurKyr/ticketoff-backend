"""platform settings (BE-26)

platform_settings: the fees a platform admin can change (activation fee, processing fee
percent, checkout hold). At most one row (id = 1); no row = the env values apply.

Revision ID: 34775f22b9f7
Revises: e7b3c9a15d84
Create Date: 2026-11-23 20:37:07.555286

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '34775f22b9f7'
down_revision: str | None = 'e7b3c9a15d84'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('platform_settings',
    sa.Column('id', sa.SmallInteger(), autoincrement=False, nullable=False),
    sa.Column('activation_fee_kzt', sa.Integer(), nullable=False),
    sa.Column('processing_fee_percent', sa.Integer(), nullable=False),
    sa.Column('checkout_hold_minutes', sa.Integer(), nullable=False),
    sa.Column('updated_by_user_id', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('activation_fee_kzt >= 1', name=op.f('ck_platform_settings_activation_fee_positive')),
    sa.CheckConstraint('checkout_hold_minutes >= 1', name=op.f('ck_platform_settings_checkout_hold_positive')),
    sa.CheckConstraint('id = 1', name=op.f('ck_platform_settings_single_row')),
    sa.CheckConstraint('processing_fee_percent BETWEEN 0 AND 100', name=op.f('ck_platform_settings_processing_fee_percent_range')),
    sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id'], name=op.f('fk_platform_settings_updated_by_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_settings'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('platform_settings')
