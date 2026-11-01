"""When the hub told the admins that an agent's pool has no node ready.

Revision ID: 0190
Revises: 0189
Create Date: 2026-11-01

worker_pools.empty_notified_at is new (plan agent-dedicated-pools, criterion a2): the moment the hub sent the admins
of the agent's project the notice ``agent_pool_empty`` because a run of the agent queued while the pool had no node
ready. It is NULL while the pool has been told of nobody; the hub sets it with the notice and clears it when a node of
the pool is ready again, so the admins hear of an outage once and of the next one again. The notice kind needs no
change of the check on notifications.notice_kind: its name has the shape of a plugin's kind.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0190"
down_revision = "0189"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_pools", sa.Column("empty_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("worker_pools", "empty_notified_at")
