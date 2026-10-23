"""The probes of the Curator's proposals and changes: a task a session got wrong, from a base commit, and a one-line
verify that exits 0 when an agent given it does not repeat the mistake (plan correction-recurrence, step 1).

Revision ID: 0130
Revises: 0129
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_curator`` and ``evo_agents.hub.probes`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

curator_probes holds one row per probe: its project, the proposal it came with and the change accepting that proposal
made (or the merged change a review run or an admin gave it), the repo, the base commit (null: the tip of the default
branch when it was made), the task, the verify, its state (active or retired), its source (proposal, review or admin)
and the label of what it was read from. Going back drops the table and the probes in it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

STATES = ("active", "retired")
SOURCES = ("proposal", "review", "admin")
MAX_TASK_BYTES = 16 * 1024
MAX_VERIFY_CHARS = 2000
MAX_NOTE_CHARS = 2000
MAX_REPO_CHARS = 200

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "curator_probes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        sa.Column("change_id", sa.BigInteger(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("verify", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_run_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_note", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_probes_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="curator_probes_proposal_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_probes_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL", name="curator_probes_created_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_run_id"], ["runs.id"], ondelete="SET NULL", name="curator_probes_created_run_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="SET NULL", name="curator_probes_updated_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_probes_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("state").in_(STATES),
                col("source").in_(SOURCES),
                sa.func.length(col("repo")).between(1, MAX_REPO_CHARS),
                sa.func.octet_length(col("task")).between(1, MAX_TASK_BYTES),
                sa.func.length(col("verify")).between(1, MAX_VERIFY_CHARS),
                col("base_sha").is_(None) | col("base_sha").regexp_match("^[0-9a-f]{7,64}$"),
                sa.func.coalesce(sa.func.length(col("retired_note")), 0) <= MAX_NOTE_CHARS,
                col("proposal_id").isnot(None) | col("change_id").isnot(None),
            ),
            name="curator_probes_check",
        ),
    )
    op.create_index("curator_probes_project_idx", "curator_probes", ["project_id", "id"])
    op.create_index("curator_probes_change_idx", "curator_probes", ["change_id"])
    op.create_index("curator_probes_proposal_idx", "curator_probes", ["proposal_id"])


def downgrade() -> None:
    op.drop_index("curator_probes_proposal_idx", table_name="curator_probes")
    op.drop_index("curator_probes_change_idx", table_name="curator_probes")
    op.drop_index("curator_probes_project_idx", table_name="curator_probes")
    op.drop_table("curator_probes")
