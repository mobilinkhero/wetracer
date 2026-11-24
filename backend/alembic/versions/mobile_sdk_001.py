"""Mobile API tables: mobile devices, push attempts, FCM config

New tables for the public REST Chat API (/api/v1/mobile):
- mobile_device_tokens: one FCM registration token per conversation
  (the visitor has no dashboard user, so the existing fcm_tokens table,
  keyed by user_id for signed-in browsers, cannot hold these).
- mobile_push_attempts: every push decision, sent or not — including the
  test-mode "recorded_no_credentials" rows written when no FCM credentials
  are configured, so the chat never fails for want of credentials.
- mobile_fcm_configs: one org's encrypted FCM service-account JSON.

Revision ID: mobile_sdk_001
Revises: note_agent_author_001
Create Date: 2026-11-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Keep revision ids under 32 characters: alembic_version.version_num is varchar(32).
revision: str = 'mobile_sdk_001'
down_revision: Union[str, None] = 'note_agent_author_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'mobile_device_tokens',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('session_id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('push_token', sa.Text(), nullable=False),
        sa.Column('platform', sa.String(16), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['session_id'], ['session_to_agents.session_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', 'push_token', name='uq_mobile_device_session_token'),
    )
    op.create_index('ix_mobile_device_tokens_session_id', 'mobile_device_tokens', ['session_id'])

    op.create_table(
        'mobile_push_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.UUID(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('device_token_prefix', sa.String(16), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['session_id'], ['session_to_agents.session_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mobile_push_attempts_session_id', 'mobile_push_attempts', ['session_id'])

    op.create_table(
        'mobile_fcm_configs',
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('encrypted_service_account_json', sa.Text(), nullable=False),
        sa.Column('project_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('organization_id'),
    )


def downgrade() -> None:
    op.drop_table('mobile_fcm_configs')
    op.drop_index('ix_mobile_push_attempts_session_id', table_name='mobile_push_attempts')
    op.drop_table('mobile_push_attempts')
    op.drop_index('ix_mobile_device_tokens_session_id', table_name='mobile_device_tokens')
    op.drop_table('mobile_device_tokens')
