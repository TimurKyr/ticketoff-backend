"""support attachments (BE-33)

New table support_attachments: the metadata of a file sent with a support message. The
bytes are in the attachment storage (docker volume), not in the database.

Revision ID: b8d2f6a41c97
Revises: e7b3c9a15d84
Create Date: 2026-11-24 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d2f6a41c97'
down_revision: str | None = 'e7b3c9a15d84'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('support_attachments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=50), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("content_type IN ('image/png', 'image/jpeg', 'application/pdf')", name=op.f('ck_support_attachments_content_type_allowed')),
    sa.CheckConstraint('size > 0 AND size <= 5242880', name=op.f('ck_support_attachments_size_within_limit')),
    sa.ForeignKeyConstraint(['message_id'], ['support_messages.id'], name=op.f('fk_support_attachments_message_id_support_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_support_attachments'))
    )
    op.create_index(op.f('ix_support_attachments_message_id'), 'support_attachments', ['message_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Only the table goes; the files in the storage folder stay (delete them by hand).
    op.drop_index(op.f('ix_support_attachments_message_id'), table_name='support_attachments')
    op.drop_table('support_attachments')
