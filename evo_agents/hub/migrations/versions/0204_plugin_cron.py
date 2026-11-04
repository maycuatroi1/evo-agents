"""The last minute the plugins' jobs were queued for.

Revision ID: 0204
Revises: 0203
Create Date: 2026-11-04

plugin_cron holds at most one row (id 1, plan code-quality-upgrade, step 3): the minute through which hub.plugin_jobs
queued the plugins' jobs whose cron takes it (``evo_agents.hub.plugin_jobs.fire``). A pass that runs late, or after the
worker was down, queues the jobs of every minute since, at most the last hour, each minute once. Until the first pass
there is no row, and the first pass queues its own minute only.

Going back drops the table: hub.plugin_jobs queues the jobs of its own minute alone again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_cron",
        sa.Column("id", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("fired_through", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="plugin_cron_pkey"),
        sa.CheckConstraint(sa.column("id") == 1, name="plugin_cron_id_check"),
    )


def downgrade() -> None:
    op.drop_table("plugin_cron")
