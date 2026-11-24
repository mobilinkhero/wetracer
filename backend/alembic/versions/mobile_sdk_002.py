"""Phase 1 API security tables: blocked visitors, widget security, abuse log

- blocked_visitors: the blocked-visitor list (org-scoped, by customer and/or
  IP) enforced by the mobile API and the widget socket (403).
- mobile_widget_security: per-widget kill switch + limit overrides.
- mobile_abuse_events: append-only abuse-control log (abuse panel backend).

Revision ID: mobile_sdk_002
Revises: mobile_sdk_001
Create Date: 2026-11-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Keep revision ids under 32 characters: alembic_version.version_num is varchar(32).
revision: str = 'mobile_sdk_002'
down_revision: Union[str, None] = 'mobile_sdk_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'blocked_visitors',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('customer_id', sa.UUID(), nullable=True),
        sa.Column('ip_address', sa.String(64), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('blocked_by_user_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['blocked_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_blocked_visitors_org_customer', 'blocked_visitors',
                    ['organization_id', 'customer_id'])
    op.create_index('ix_blocked_visitors_org_ip', 'blocked_visitors',
                    ['organization_id', 'ip_address'])

    op.create_table(
        'mobile_widget_security',
        sa.Column('widget_id', sa.String(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('chat_disabled', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('msg_per_min', sa.Integer(), nullable=True),
        sa.Column('msg_per_ip_per_min', sa.Integer(), nullable=True),
        sa.Column('msg_per_widget_per_min', sa.Integer(), nullable=True),
        sa.Column('ai_daily_quota_per_conversation', sa.Integer(), nullable=True),
        sa.Column('ai_daily_quota_per_widget', sa.Integer(), nullable=True),
        sa.Column('upload_files_per_day', sa.Integer(), nullable=True),
        sa.Column('upload_bytes_per_day', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('widget_id'),
    )

    op.create_table(
        'mobile_abuse_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=True),
        sa.Column('widget_id', sa.String(), nullable=True),
        sa.Column('session_id', sa.UUID(), nullable=True),
        sa.Column('ip_address', sa.String(64), nullable=True),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['session_to_agents.session_id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mobile_abuse_events_org_created', 'mobile_abuse_events',
                    ['organization_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_mobile_abuse_events_org_created', table_name='mobile_abuse_events')
    op.drop_table('mobile_abuse_events')
    op.drop_table('mobile_widget_security')
    op.drop_index('ix_blocked_visitors_org_ip', table_name='blocked_visitors')
    op.drop_index('ix_blocked_visitors_org_customer', table_name='blocked_visitors')
    op.drop_table('blocked_visitors')
