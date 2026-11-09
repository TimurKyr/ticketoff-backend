"""refine remaining entities (BE-10 review)

Revision ID: a4d1c7e90b52
Revises: 3e867ef67100
Create Date: 2026-11-09 17:30:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a4d1c7e90b52'
down_revision: str | None = '3e867ef67100'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # created_at on orders and tickets. Existing rows get now(): their real creation time
    # was never stored.
    op.add_column('orders', sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    op.add_column('tickets', sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))

    # A seated order item is exactly one seat.
    op.create_check_constraint(op.f('ck_order_items_seat_item_quantity_one'), 'order_items', 'seat_id IS NULL OR quantity = 1')

    # A sold seat must say which order bought it.
    op.create_check_constraint(op.f('ck_seat_holds_converted_has_order'), 'seat_holds', "status <> 'converted' OR order_id IS NOT NULL")

    # Payments and refunds move real (simulated) money: zero is not a valid amount.
    op.drop_constraint(op.f('ck_payments_amount_non_negative'), 'payments', type_='check')
    op.create_check_constraint(op.f('ck_payments_amount_positive'), 'payments', 'amount_kzt > 0')
    op.drop_constraint(op.f('ck_refunds_amount_non_negative'), 'refunds', type_='check')
    op.create_check_constraint(op.f('ck_refunds_amount_positive'), 'refunds', 'amount_kzt > 0')

    # Campaign window, so PromoCodeStatus.expired has something to expire against.
    op.add_column('promotional_campaigns', sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('promotional_campaigns', sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(op.f('ck_promotional_campaigns_window_valid'), 'promotional_campaigns', 'starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_promotional_campaigns_window_valid'), 'promotional_campaigns', type_='check')
    op.drop_column('promotional_campaigns', 'ends_at')
    op.drop_column('promotional_campaigns', 'starts_at')

    op.drop_constraint(op.f('ck_refunds_amount_positive'), 'refunds', type_='check')
    op.create_check_constraint(op.f('ck_refunds_amount_non_negative'), 'refunds', 'amount_kzt >= 0')
    op.drop_constraint(op.f('ck_payments_amount_positive'), 'payments', type_='check')
    op.create_check_constraint(op.f('ck_payments_amount_non_negative'), 'payments', 'amount_kzt >= 0')

    op.drop_constraint(op.f('ck_seat_holds_converted_has_order'), 'seat_holds', type_='check')
    op.drop_constraint(op.f('ck_order_items_seat_item_quantity_one'), 'order_items', type_='check')

    op.drop_column('tickets', 'created_at')
    op.drop_column('orders', 'created_at')
