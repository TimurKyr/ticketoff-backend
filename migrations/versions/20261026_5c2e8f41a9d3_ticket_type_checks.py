"""ticket type checks

Revision ID: 5c2e8f41a9d3
Revises: 763991b0391a
Create Date: 2026-10-26 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '5c2e8f41a9d3'
down_revision: str | None = '763991b0391a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written by hand: autogenerate does not detect CHECK constraints.
# Keep in sync with TicketType.__table_args__.
CHECKS = {
    'ck_ticket_types_price_matches_kind':
        "(kind = 'free' AND price_kzt = 0) OR (kind = 'paid' AND price_kzt > 0)",
    'ck_ticket_types_quantity_total_positive': 'quantity_total > 0',
    'ck_ticket_types_max_per_order_positive': 'max_per_order IS NULL OR max_per_order >= 1',
    'ck_ticket_types_max_per_order_within_total':
        'max_per_order IS NULL OR max_per_order <= quantity_total',
    'ck_ticket_types_sales_window_valid':
        'sales_start_at IS NULL OR sales_end_at IS NULL OR sales_start_at < sales_end_at',
}


def upgrade() -> None:
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(name), 'ticket_types', condition)


def downgrade() -> None:
    for name in reversed(list(CHECKS)):
        op.drop_constraint(op.f(name), 'ticket_types', type_='check')
