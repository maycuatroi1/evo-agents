"""Cron jobs: when a job was resumed last.

Revision ID: 0245
Revises: 0244
Create Date: 2026-11-12

Plan hub-cron-jobs, criteria a2 and a6 (``evo_agents.hub.server.cron_fire``). cron_jobs gain ``resumed_at``: a job
resumed fires no minute it was due at before it, and its breaker counts only the runs queued since. NULL for a job
never resumed, which counts from its ``created_at``.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0245"
down_revision = "0244"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cron_jobs", sa.Column("resumed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("cron_jobs", "resumed_at")
