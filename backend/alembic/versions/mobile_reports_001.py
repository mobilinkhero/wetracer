"""Visitor reports of offensive AI content (mobile_reports table)

Revision ID: mobile_reports_001
Revises: allow_attachments_001
Create Date: 2026-11-26

Google Play's AI-Generated Content policy requires apps with generative AI
to let users report or flag offensive AI content without leaving the app.
POST /api/v1/mobile/reports stores each report here (widget, org, reason,
details, source, client IP); org admins read them through
GET /api/v1/admin/mobile/reports. No data to backfill — the endpoint and
the table arrive together.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'mobile_reports_001'
down_revision: Union[str, None] = 'allow_attachments_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'mobile_reports',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('widget_id', sa.String(), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reason', sa.String(64), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('source', sa.String(64), nullable=True),
        sa.Column('ip_address', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mobile_reports_widget_id', 'mobile_reports', ['widget_id'])
    op.create_index('ix_mobile_reports_organization_id', 'mobile_reports', ['organization_id'])
    op.create_index('ix_mobile_reports_created_at', 'mobile_reports', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_mobile_reports_created_at', table_name='mobile_reports')
    op.drop_index('ix_mobile_reports_organization_id', table_name='mobile_reports')
    op.drop_index('ix_mobile_reports_widget_id', table_name='mobile_reports')
    op.drop_table('mobile_reports')
