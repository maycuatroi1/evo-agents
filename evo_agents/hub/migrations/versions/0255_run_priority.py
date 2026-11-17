"""The priority of a run: runs.priority.

Revision ID: 0255
Revises: 0254
Create Date: 2026-11-16

Plan orchestrator-keeps-plans-moving, step 1: a mention or an agent ask of an agent whose pool has no free slot queues
its chat run instead of refusing it, and a worker claims the queued runs by priority first, then by id. A run's
priority is one of urgent, high, medium and low (``evo_agents.hub.runs.PRIORITIES``); the asker sets it with
``evo-agents worker agent ask --priority`` and a writer of the project changes it while the run is queued
(``evo-agents hub run priority``). The column takes the constant default medium, so adding it rewrites no row and
every run already there, plan and step runs among them, keeps its place in the queue.

Going back drops the column; the queue is in id order again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0255"
down_revision = "0254"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("priority", sa.Text(), nullable=False, server_default="medium"))


def downgrade() -> None:
    op.drop_column("runs", "priority")
