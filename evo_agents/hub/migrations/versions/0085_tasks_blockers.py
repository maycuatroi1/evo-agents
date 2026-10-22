"""What a task waits on, and when a sprint turned active: tasks_blockers and tasks_sprints.activated_at.

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_tasks`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A blocker is a flag beside a task's status, not a status (plan pm-daily-tracking, criterion a1). tasks_blockers holds
one row per blocker: the task, why (``reason``), who or what it waits for in exactly one of three forms (a member:
``waiting_user_id``; another task: ``waiting_task_id``, never the task itself; a text for someone outside:
``waiting_text``), since when, who set it, and, once cleared, when and by whom (both or neither). A task has as many
open blockers (``cleared_at`` NULL) as it has rows. tasks_sprints.activated_at is when the sprint turned active; a
sprint active before this revision, or not yet, keeps it empty.

Going back drops the column and the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

TABLE = "tasks_blockers"
MAX_REASON_CHARS = 1000  # evo_agents.hub.tables_tasks.MAX_REASON_CHARS

col = sa.column


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    waiting_user, waiting_task, waiting_text = col("waiting_user_id"), col("waiting_task_id"), col("waiting_text")
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("waiting_user_id", sa.BigInteger(), nullable=True),
        sa.Column("waiting_task_id", sa.BigInteger(), nullable=True),
        sa.Column("waiting_text", sa.Text(), nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.BigInteger(), nullable=True),
        _key("task_id", "tasks_items", ondelete="CASCADE"),
        _key("waiting_user_id", "users", ondelete="RESTRICT"),
        _key("waiting_task_id", "tasks_items", ondelete="CASCADE"),
        _key("created_by", "users", ondelete="RESTRICT"),
        _key("cleared_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(
            sa.func.num_nonnulls(waiting_user, waiting_task, waiting_text) == 1, name=f"{TABLE}_waiting_check"
        ),
        sa.CheckConstraint(sa.or_(waiting_task.is_(None), waiting_task != col("task_id")), name=f"{TABLE}_self_check"),
        sa.CheckConstraint(
            sa.func.char_length(col("reason")).between(1, MAX_REASON_CHARS), name=f"{TABLE}_reason_check"
        ),
        sa.CheckConstraint(col("cleared_at").is_(None) == col("cleared_by").is_(None), name=f"{TABLE}_cleared_check"),
    )
    op.create_index(f"{TABLE}_task_idx", TABLE, ["task_id", "id"])
    op.create_index(f"{TABLE}_open_idx", TABLE, ["task_id"], postgresql_where=col("cleared_at").is_(None))
    op.add_column("tasks_sprints", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks_sprints", "activated_at")
    op.drop_table(TABLE)
