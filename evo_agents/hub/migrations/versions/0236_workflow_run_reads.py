"""workflow_run_reads: the hub's latest read of each run of a GitHub Actions workflow a wait names.

Revision ID: 0236
Revises: 0235
Create Date: 2026-11-11

The poll of the reaper (``evo_agents.hub.server.workflow_waits.poll``) reads each run of a workflow an open wait names
once a pass; it now keeps what it read, the run with the jobs of its latest attempt and their steps, so the views of
a wait show where the run stands without a call to GitHub when someone looks. A row is a project's run of a repo
(owner/name); ``data`` holds the read, ``read_at`` when it was made, ``typical_seconds`` how long recent runs of the
workflow that succeeded took and ``typical_read_at`` when the hub read those (NULL: not yet, or not known). The poll
drops rows it has not read for a while. A new table, so the code before it runs on the schema after it.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0236"
down_revision = "0235"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_run_reads",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("typical_seconds", sa.Integer(), nullable=True),
        sa.Column("typical_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="workflow_run_reads_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "repo", "run_id", name="workflow_run_reads_pkey"),
    )
    op.create_index("workflow_run_reads_read_idx", "workflow_run_reads", ["read_at"])


def downgrade() -> None:
    op.drop_index("workflow_run_reads_read_idx", table_name="workflow_run_reads")
    op.drop_table("workflow_run_reads")
