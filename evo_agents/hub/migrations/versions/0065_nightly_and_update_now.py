"""The commit of the tag nightly the hub last read, and an owner's Update now of a worker.

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-20

hub_nightly holds at most one row (id 1): the commit the tag nightly of evo-agents pointed to when the hub's job
hub.read_nightly last read it, 40 or 64 hexadecimal digits (OBJECT_NAME), and when that was. A read that fails leaves
the row as it was; until the first read succeeds there is none. The heartbeat answer hands both to every daemon.

workers.update_requested_at is when the worker's owner, or a hub admin, last asked its daemon to update itself now
(POST /v1/workers/{id}/update-now); update_ended_at is when that request ended, NULL while it is open and when none
was ever asked for; update_outcome says how (current, cancelled, failed or impossible) and update_reason why, when the
daemon said. An outcome goes with an end and an end with a request. The bounds below are copies of
``evo_agents.hub.server.runs.models.OBJECT_NAME`` and ``evo_agents.hub.runs.UPDATE_NOW_OUTCOMES`` as of this revision,
never imports, so a later change to them takes a new revision.

Going back drops the table and the four columns, which is the schema 0064 runs on: its hub reads no nightly and takes
no Update now.
"""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

OBJECT_NAME = "^([0-9a-f]{40}|[0-9a-f]{64})$"  # evo_agents.hub.server.runs.models.OBJECT_NAME
OUTCOMES = ("current", "cancelled", "failed", "impossible")  # evo_agents.hub.runs.UPDATE_NOW_OUTCOMES
COLUMNS = ("update_requested_at", "update_ended_at", "update_outcome", "update_reason")
CHECK = "workers_update_now_check"

col = sa.column


def upgrade() -> None:
    op.create_table(
        "hub_nightly",
        sa.Column("id", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="hub_nightly_pkey"),
        sa.CheckConstraint(col("id") == 1, name="hub_nightly_id_check"),
        sa.CheckConstraint(col("commit_sha").regexp_match(OBJECT_NAME), name="hub_nightly_commit_sha_check"),
    )
    op.add_column("workers", sa.Column("update_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("update_ended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("update_outcome", sa.Text(), nullable=True))
    op.add_column("workers", sa.Column("update_reason", sa.Text(), nullable=True))
    ended, outcome = col("update_ended_at"), col("update_outcome")
    shape = sa.and_(
        sa.or_(ended.is_(None), col("update_requested_at").is_not(None)),
        # outcome IS NOT NULL first: a NULL outcome makes IN NULL, and a CHECK passes on NULL
        sa.or_(
            sa.and_(ended.is_(None), outcome.is_(None)),
            sa.and_(ended.is_not(None), outcome.is_not(None), outcome.in_(OUTCOMES)),
        ),
        sa.or_(col("update_reason").is_(None), outcome.is_not(None)),
    )
    op.create_check_constraint(CHECK, "workers", shape)


def downgrade() -> None:
    op.drop_constraint(CHECK, "workers", type_="check")
    for name in reversed(COLUMNS):
        op.drop_column("workers", name)
    op.drop_table("hub_nightly")
