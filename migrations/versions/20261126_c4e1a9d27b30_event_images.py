"""event images (R-12)

The cover image of an event: events.image_id (the file's key in the image storage) and
events.image_content_type. Both NULL (no image) or both set. The bytes are in the image
storage (a folder of the attachments volume), not in the database.

Revision ID: c4e1a9d27b30
Revises: 703f7fea8054
Create Date: 2026-11-26 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e1a9d27b30"
down_revision: str | None = "703f7fea8054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("events", sa.Column("image_id", sa.UUID(), nullable=True))
    op.add_column("events", sa.Column("image_content_type", sa.String(length=20), nullable=True))
    op.create_check_constraint(
        op.f("ck_events_image_fields_together"),
        "events",
        "(image_id IS NULL) = (image_content_type IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_events_image_content_type_allowed"),
        "events",
        "image_content_type IN ('image/png', 'image/jpeg', 'image/webp')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_events_image_content_type_allowed"), "events", type_="check")
    op.drop_constraint(op.f("ck_events_image_fields_together"), "events", type_="check")
    op.drop_column("events", "image_content_type")
    op.drop_column("events", "image_id")
