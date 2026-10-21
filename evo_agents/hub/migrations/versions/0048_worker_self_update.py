"""A worker's version lock, and the commit its daemon runs with what it found of updating itself to the tag nightly.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-19

workers.version_locked is the lock its owner sets with PATCH /v1/workers/{id}: true, its daemon stays on the
evo-agents it runs; false, the default for every worker there is and every new one, it updates itself to the tag
nightly once all the daemons of its install are idle. The heartbeat answer carries it in settings. workers.agent_commit
is the commit of evo-agents the worker's last heartbeat said its install was built from, 40 or 64 hexadecimal digits
(OBJECT_NAME), NULL when it said none. workers.self_update is what that heartbeat said of updating itself, an object
(``evo_agents.hub.server.runs.models.SelfUpdateReport``), NULL when it said nothing, as an older daemon does;
workers.self_update_since is when the hub first got the state that object holds, in an unbroken line of heartbeats
that said it. The bound below is a copy of ``evo_agents.hub.server.runs.models.OBJECT_NAME`` as of this revision,
never an import, so a later change to it takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions, as ``evo_agents.hub.tables`` describes the table.

Going back drops the four columns, which is the schema that schema 0047 runs on: no worker is locked there, and its
heartbeat neither takes nor keeps a commit or a state of updating.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

OBJECT_NAME = "^([0-9a-f]{40}|[0-9a-f]{64})$"  # evo_agents.hub.server.runs.models.OBJECT_NAME
CHECKS = ("workers_agent_commit_check", "workers_self_update_check")
COLUMNS = ("version_locked", "agent_commit", "self_update", "self_update_since")

col = sa.column


def upgrade() -> None:
    op.add_column("workers", sa.Column("version_locked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("workers", sa.Column("agent_commit", sa.Text(), nullable=True))
    op.add_column("workers", sa.Column("self_update", postgresql.JSONB(), nullable=True))
    op.add_column("workers", sa.Column("self_update_since", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(CHECKS[0], "workers", col("agent_commit").regexp_match(OBJECT_NAME))
    op.create_check_constraint(CHECKS[1], "workers", sa.func.jsonb_typeof(col("self_update")) == "object")


def downgrade() -> None:
    for name in CHECKS:
        op.drop_constraint(name, "workers", type_="check")
    for name in reversed(COLUMNS):
        op.drop_column("workers", name)
