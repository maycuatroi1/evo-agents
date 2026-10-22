"""The upkeep of agents' memories: when one was last opened, archived, or found stale.

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-22

memories takes three timestamps, all NULL for every memory there is (plan agent-profile-workspace, criterion a3):
read_at, when the agent of a run last opened the memory with memory_get; archived_at, when the hub's daily job archived
an agent's memory nobody opened for 30 days; stale_at, when that job found a path the memory cites (code:REPO:PATH)
gone from the project's knowledge graph.

The names below are copies of ``evo_agents.hub.tables_memory_agents`` as of this revision, never imports, so a later
change to it takes a new revision. Every statement is an Alembic operation.

Going back drops the three columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

UPKEEP = ("read_at", "archived_at", "stale_at")


def upgrade() -> None:
    for name in UPKEEP:
        op.add_column("memories", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in reversed(UPKEEP):
        op.drop_column("memories", name)
