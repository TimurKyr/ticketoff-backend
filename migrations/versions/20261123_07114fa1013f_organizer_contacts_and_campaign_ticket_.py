"""organizer contacts and campaign ticket types (BE-29)

Revision ID: 07114fa1013f
Revises: c4a1e7d93b20
Create Date: 2026-11-23 11:42:53.168298

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07114fa1013f'
down_revision: str | None = 'c4a1e7d93b20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Ticket types a campaign applies to; no rows = every type of the campaign's event.
    op.create_table('campaign_ticket_types',
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('ticket_type_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['promotional_campaigns.id'], name=op.f('fk_campaign_ticket_types_campaign_id_promotional_campaigns'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_type_id'], ['ticket_types.id'], name=op.f('fk_campaign_ticket_types_ticket_type_id_ticket_types'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('campaign_id', 'ticket_type_id', name=op.f('pk_campaign_ticket_types'))
    )
    op.create_index(op.f('ix_campaign_ticket_types_ticket_type_id'), 'campaign_ticket_types', ['ticket_type_id'], unique=False)
    op.add_column('organizer_profiles', sa.Column('contact_email', sa.String(length=254), nullable=True))
    op.add_column('organizer_profiles', sa.Column('contact_phone', sa.String(length=16), nullable=True))
    op.add_column('organizer_profiles', sa.Column('website', sa.String(length=255), nullable=True))
    # The API validates the formats; these keep the stored forms honest too.
    op.create_check_constraint(
        op.f('ck_organizer_profiles_contact_phone_format'),
        'organizer_profiles',
        "contact_phone IS NULL OR contact_phone ~ '^\\+[0-9]{10,15}$'",
    )
    op.create_check_constraint(
        op.f('ck_organizer_profiles_website_http'),
        'organizer_profiles',
        "website IS NULL OR website ~ '^https?://'",
    )
    op.create_check_constraint(
        op.f('ck_organizer_profiles_contact_email_lowercase'),
        'organizer_profiles',
        'contact_email IS NULL OR contact_email = lower(contact_email)',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_organizer_profiles_contact_email_lowercase'), 'organizer_profiles', type_='check')
    op.drop_constraint(op.f('ck_organizer_profiles_website_http'), 'organizer_profiles', type_='check')
    op.drop_constraint(op.f('ck_organizer_profiles_contact_phone_format'), 'organizer_profiles', type_='check')
    op.drop_column('organizer_profiles', 'website')
    op.drop_column('organizer_profiles', 'contact_phone')
    op.drop_column('organizer_profiles', 'contact_email')
    op.drop_index(op.f('ix_campaign_ticket_types_ticket_type_id'), table_name='campaign_ticket_types')
    op.drop_table('campaign_ticket_types')
