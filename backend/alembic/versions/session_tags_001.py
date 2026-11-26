"""AI auto-tagging for conversations: the canonical tag taxonomy.

One fixed list, shared by the tagger (which assigns tags from message
content), the API (which validates them) and the inbox (which sorts and
filters by them). The slug is the stored value — session_to_agents.tags holds
a JSON list of slugs; the label, group, weight and colour live here only, so
renaming a label never rewrites stored rows.

Priority: the inbox sorts by the highest weight among a conversation's tags
(see priority_score). Payment-received sits at the top on purpose — money is
in and the result is not delivered yet, so that chat waits on us, not the
other way round.

Revision ID: session_tags_001
Revises: mobile_reports_001
Create Date: 2026-11-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# Keep revision ids under 32 characters: alembic_version.version_num is
# varchar(32).
revision: str = 'session_tags_001'
down_revision: Union[str, None] = 'mobile_reports_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('session_to_agents', sa.Column('tags', sa.JSON(), nullable=True),
                  if_not_exists=True)


def downgrade() -> None:
    op.drop_column('session_to_agents', 'tags', if_exists=True)
