"""The default system of a project, the pause of its ops, and the queue of a project's operations events.

Revision ID: 0237
Revises: 0236
Create Date: 2026-11-11

The names below are copies of ``evo_agents.hub.tables_ops`` and ``evo_agents.hub.ops_queue`` as of this revision,
never imports, so a later change to them takes a new revision.

ops_cursors holds one row per project whose queue ever read (plan hub-steward, criterion a7): the cursor on
hub_events. ops_batches holds each batch of a cause key: its kind, cause and subject, its state (open, then ready or
empty; at most one open per key), when it opened, when it closes and closed, and the run opened for it. ops_items holds
each operations event a batch took, one per event: the ids it names, pending until tier 0 handled it. A project's rows
go with the project; a run or event deleted under them leaves them, the link cleared.

agent_systems gains is_default (at most one per project: agent_systems_default_key) and ops_paused_at with
ops_paused_reason (its ops hold their batches while set). projects gains systems_seeded_at: when the hub gave the
project its ``orchestrator`` system, or found it had one; a project the hub seeded once is not seeded again, so
deleting its default leaves it with none. Here the oldest system of each project becomes its default and each
project with a system counts as seeded; the hub seeds the others.

Going back drops the three tables and the new columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None

BATCH_STATES = ("open", "ready", "empty", "running", "done")
ITEM_STATES = ("pending", "handled")
TABLES = ("ops_items", "ops_batches", "ops_cursors")  # in the order going back drops them
SYSTEM_COLUMNS = ("ops_paused_reason", "ops_paused_at", "is_default")

col = sa.column


def _key(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _cursors() -> None:
    name = "ops_cursors"
    op.create_table(
        name,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), server_default="0", nullable=False),
        _stamp("updated_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", name="ops_cursors_pkey"),
        sa.CheckConstraint(col("cursor") >= 0, name="ops_cursors_cursor_check"),
    )


def _batches() -> None:
    name = "ops_batches"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        _stamp("opened_at"),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        _when("closed_at"),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="ops_batches_pkey"),
        sa.CheckConstraint(col("state").in_(BATCH_STATES), name="ops_batches_state_check"),
        sa.CheckConstraint((col("state") == "open") == col("closed_at").is_(None), name="ops_batches_closed_at_check"),
    )
    op.create_index(
        "ops_batches_open_key", name, ["project_id", "key"], unique=True, postgresql_where=sa.text("state = 'open'")
    )
    op.create_index("ops_batches_project_idx", name, ["project_id", "id"])


def _items() -> None:
    name = "ops_items"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("worker_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("handled_by", sa.Text(), nullable=True),
        _stamp("created_at"),
        _when("handled_at"),
        _key(name, "batch_id", "ops_batches", ondelete="CASCADE"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "event_id", "hub_events", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="ops_items_pkey"),
        sa.CheckConstraint(col("state").in_(ITEM_STATES), name="ops_items_state_check"),
    )
    op.create_index("ops_items_event_key", name, ["event_id"], unique=True)
    op.create_index("ops_items_batch_idx", name, ["batch_id"])
    op.create_index(
        "ops_items_pending_idx", name, ["project_id", "run_id"], postgresql_where=sa.text("state = 'pending'")
    )


def _defaults() -> None:
    op.add_column("agent_systems", sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("agent_systems", _when("ops_paused_at"))
    op.add_column("agent_systems", sa.Column("ops_paused_reason", sa.Text(), nullable=True))
    op.create_index(
        "agent_systems_default_key",
        "agent_systems",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.add_column("projects", _when("systems_seeded_at"))
    op.execute(
        "UPDATE agent_systems SET is_default = true WHERE id IN (SELECT min(id) FROM agent_systems GROUP BY project_id)"
    )
    op.execute(
        "UPDATE projects SET systems_seeded_at = now() "
        "WHERE EXISTS (SELECT 1 FROM agent_systems s WHERE s.project_id = projects.id)"
    )


def upgrade() -> None:
    _defaults()
    _cursors()
    _batches()
    _items()


def downgrade() -> None:
    for name in TABLES:
        op.drop_table(name)
    op.drop_column("projects", "systems_seeded_at")
    op.drop_index("agent_systems_default_key", table_name="agent_systems")
    for name in SYSTEM_COLUMNS:
        op.drop_column("agent_systems", name)
