"""The runs of the KG optimize loop.

Revision ID: 0182
Revises: 0181
Create Date: 2026-10-28

kg_optimize_runs is new (plan kg-retrieval-quality, criteria a7 and a9): one row each time the hub optimizes a
project's knowledge graph (``evo_agents.hub.server.kg_optimize``), each night of the night shift (``via`` night, one a
project and night) or when a writer asks (``via`` manual). Its ``state`` walks measuring, running, after, then done,
reverted or failed; one run a project is in flight at a time (``kg_optimize_runs_active_key``), so a second call finds
the first. The review run of the agent on duty it queued, the builds measured before and after, the weight of the
asked column the run left in effect (``asked_weight``, null when it changed none), what it measured, decided and undid
(``body``), and the report sent to the owner with its notification.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0182"
down_revision = "0181"
branch_labels = None
depends_on = None

TABLE = "kg_optimize_runs"
ACTIVE = ("measuring", "running", "after")
STATES = (*ACTIVE, "done", "reverted", "failed")


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    col = sa.column
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("night", sa.Date(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("before_build_id", sa.BigInteger(), nullable=True),
        sa.Column("after_build_id", sa.BigInteger(), nullable=True),
        sa.Column("asked_weight", sa.Double(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("notification_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _key("project_id", "projects", ondelete="CASCADE"),
        _key("requested_by", "users", ondelete="SET NULL"),
        _key("agent_run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(col("via").in_(("night", "manual")), name=f"{TABLE}_via_check"),
        sa.CheckConstraint(col("state").in_(STATES), name=f"{TABLE}_state_check"),
    )
    op.create_index(f"{TABLE}_project_idx", TABLE, ["project_id", "id"])
    op.create_index(
        f"{TABLE}_active_key", TABLE, ["project_id"], unique=True, postgresql_where=col("state").in_(ACTIVE)
    )
    op.create_index(
        f"{TABLE}_night_key", TABLE, ["project_id", "night"], unique=True, postgresql_where=col("via") == "night"
    )
    op.create_index(f"{TABLE}_agent_run_idx", TABLE, ["agent_run_id"])


def downgrade() -> None:
    op.drop_table(TABLE)
