"""The turn of the hub's merge queue: one merge request of a target branch at a time, and its rebases in a row.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.merge_gate`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A merge request of the queue whose gates passed waits for its turn on its target branch, and one of a branch has the
turn at a time: rebased onto the branch, its pipeline at the new head, merged with that head (plan gitlab-merge-queue,
criterion a5). The unique index merge_queue_turn_key holds that whatever moves the queue: a second merge request of
the same project, repo and target branch in a turn state (TURN_STATES) is refused. merge_queue.rebases counts the
rebases of a merge request since it last passed its gates; the queue blocks one the target branch moved under too
many times in a row. No column of a table older than 0075 changes.

Going back drops the index and the column: the hub of 0075 moves no merge request past ready.
"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

TURN_STATES = ("rebasing", "testing", "merging")  # evo_agents.hub.merge_gate.TURN_STATES

col = sa.column


def upgrade() -> None:
    op.add_column("merge_queue", sa.Column("rebases", sa.SmallInteger(), server_default="0", nullable=False))
    op.create_check_constraint("merge_queue_rebases_check", "merge_queue", col("rebases") >= 0)
    op.create_index(
        "merge_queue_turn_key",
        "merge_queue",
        ["project_id", "repo", "target_branch"],
        unique=True,
        postgresql_where=col("state").in_(TURN_STATES),
    )


def downgrade() -> None:
    op.drop_index("merge_queue_turn_key", table_name="merge_queue")
    op.drop_constraint("merge_queue_rebases_check", "merge_queue", type_="check")
    op.drop_column("merge_queue", "rebases")
