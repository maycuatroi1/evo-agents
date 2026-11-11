"""Plan run choices: a member's last choice in the Run plan dialog of a project.

Revision ID: 0237
Revises: 0236
Create Date: 2026-11-12

The names below are copies of ``evo_agents.hub.tables_push`` as of this revision, never imports, so a later change to
them takes a new revision.

plan_run_choices holds one row a member and project (plan run-plan-in-system, criterion a3): where the plan runs
(``target``: worker, a plan run on a worker, or system, the plan handed to the entry agent of the system ``system``
names), the agent by handle (NULL: the project's default), the runtime, the model (NULL: the runtime's own), the mode,
the timeout in hours and whether each step gets a session of its own; never as_me, browser identities or an account.
GET and PUT /v1/projects/{p}/plan-run-choice read and write the caller's own (``evo_agents.hub.server.plan_run_choice``),
which checks what a row names against the project as it is when read. A row goes with its member and its project.

The table is new and nothing reads it before this revision. Going back drops it; the choices are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None

TABLE = "plan_run_choices"
TARGETS = ("worker", "system")  # evo_agents.hub.tables_push.TARGETS

col = sa.column


def _key(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="CASCADE", name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("system", sa.Text(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("timeout_h", sa.Integer(), nullable=False),
        sa.Column("session_per_step", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("user_id", "users"),
        _key("project_id", "projects"),
        sa.PrimaryKeyConstraint("user_id", "project_id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(
            sa.and_(col("target").in_(TARGETS), (col("target") == "system") == col("system").is_not(None)),
            name=f"{TABLE}_check",
        ),
    )
    op.create_index(f"{TABLE}_project_idx", TABLE, ["project_id"])


def downgrade() -> None:
    op.drop_table(TABLE)
