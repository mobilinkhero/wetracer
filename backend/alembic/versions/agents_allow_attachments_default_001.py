"""Enable file attachments on all existing agents

Revision ID: agents_allow_attachments_default_001
Revises: mobile_sdk_002
Create Date: 2026-11-25

The widget's attach + mic buttons are gated on the agent's
allow_attachments flag, which defaulted to False — so every agent
created before this change shows a composer with no media upload
options, even though the AI can read images, PDFs and voice notes.
New agents get the flag on by default (model + schema default True);
this migration turns it on for the agents that already exist, so the
next update.sh run makes their widgets show the upload buttons.

Downgrade is a no-op by design: we cannot tell which agents had the
flag on deliberately before the migration, so we never turn it off.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'agents_allow_attachments_default_001'
down_revision: Union[str, None] = 'mobile_sdk_002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE agents SET allow_attachments = TRUE WHERE allow_attachments = FALSE")


def downgrade() -> None:
    # Intentional no-op: do not switch any agent's uploads back off.
    pass
