"""A local run's spend from the transcripts of its session: runs.spend_transcripts.

Revision ID: 0211
Revises: 0210
Create Date: 2026-11-06

A local run (a Claude Code session on a member's machine) whose machine sent no usage reports, all of them before the
evo-hub plugin read the transcript's usage, had no spend. The job hub.local_spend (``evo_agents.hub.local_spend``)
reads it from the transcripts the hub holds of the session, and keeps their fingerprint here so it reads them again
only when they change. NULL: never read.

Going back drops the column; the rows it wrote stay in run_spend.
"""

import sqlalchemy as sa
from alembic import op

revision = "0211"
down_revision = "0210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("spend_transcripts", sa.Text()))


def downgrade() -> None:
    op.drop_column("runs", "spend_transcripts")
