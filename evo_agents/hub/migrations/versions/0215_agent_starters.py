"""The starters of an agent profile: agent_profiles.starters.

Revision ID: 0215
Revises: 0214
Create Date: 2026-11-07

The chat home of the web (plan chat-first-home, step 1) offers a person the starters of the agent they picked before
their first message to it: at most ``profiles.MAX_STARTERS`` questions of at most ``profiles.MAX_STARTER_CHARS``
characters each, which the project's admins edit like the rest of the profile. Every profile there is, the default ones
included, starts with none: ``[]``. Its version rows stay as they were; one without the key reads as none.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0215"
down_revision = "0214"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_profiles", sa.Column("starters", JSONB(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("agent_profiles", "starters")
