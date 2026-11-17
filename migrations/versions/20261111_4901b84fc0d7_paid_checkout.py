"""paid checkout and activation (BE-14)

Revision ID: 4901b84fc0d7
Revises: a4d1c7e90b52
Create Date: 2026-11-11 17:30:40.054642

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4901b84fc0d7'
down_revision: str | None = 'a4d1c7e90b52'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Which order item an attendee's ticket belongs to: paid orders issue tickets only on
    # payment, so the link must be stored. Existing (BE-13) attendees stay NULL.
    op.add_column('attendees', sa.Column('order_item_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_attendees_order_item_id'), 'attendees', ['order_item_id'], unique=False)
    op.create_foreign_key(op.f('fk_attendees_order_item_id_order_items'), 'attendees', 'order_items', ['order_item_id'], ['id'])

    # Activation checklist: when the organizer accepted the paid-sales terms.
    op.add_column('events', sa.Column('paid_terms_accepted_at', sa.DateTime(timezone=True), nullable=True))

    # At most one succeeded payment per order and per event activation (double-pay guard).
    op.create_index('uq_payments_activation_succeeded', 'payments', ['event_id'], unique=True, postgresql_where=sa.text("status = 'succeeded' AND purpose = 'activation'"))
    op.create_index('uq_payments_order_succeeded', 'payments', ['order_id'], unique=True, postgresql_where=sa.text("status = 'succeeded' AND purpose = 'ticket_order'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_payments_order_succeeded', table_name='payments')
    op.drop_index('uq_payments_activation_succeeded', table_name='payments')

    op.drop_column('events', 'paid_terms_accepted_at')

    op.drop_constraint(op.f('fk_attendees_order_item_id_order_items'), 'attendees', type_='foreignkey')
    op.drop_index(op.f('ix_attendees_order_item_id'), table_name='attendees')
    op.drop_column('attendees', 'order_item_id')
