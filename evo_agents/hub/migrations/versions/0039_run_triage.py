"""Triage of failed runs: the group chat of a plan's failures, the thread of each failed run, and a switch per project.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-18

``evo_agents.hub.tables_triage`` describes the tables. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

triage_chats holds the chat the hub opened for the triage of a plan's failed runs, or of a project's when the run has no
plan: one per (project, plan), a NULL plan counting as one value. run_triage holds the thread of one failed run, once:
its chat, the first message, the cause and whether the hub guessed it, the chat run that message started, and the
counters of a chain of reruns. projects.triage_enabled, true by default, switches the thread off for a project.

Going back drops the two tables and the column: the threads are chat messages and stay in their chats.
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    op.add_column("projects", sa.Column("triage_enabled", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.create_table(
        "triage_chats",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="triage_chats_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE", name="triage_chats_chat_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="triage_chats_pkey"),
        sa.UniqueConstraint("chat_id", name="triage_chats_chat_id_key"),
        sa.UniqueConstraint("project_id", "plan_id", name="triage_chats_plan_key", postgresql_nulls_not_distinct=True),
    )
    op.create_table(
        "run_triage",
        _id(),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.Column("cause_inferred", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("chain_depth", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("auto_reruns", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_triage_run_id_fkey"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE", name="run_triage_chat_id_fkey"),
        sa.ForeignKeyConstraint(
            ["root_message_id"], ["chat_messages.id"], ondelete="CASCADE", name="run_triage_root_message_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["runs.id"], ondelete="SET NULL", name="run_triage_agent_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="run_triage_pkey"),
        sa.UniqueConstraint("run_id", name="run_triage_run_id_key"),
    )
    op.create_index("run_triage_chat_idx", "run_triage", ["chat_id"])


def downgrade() -> None:
    op.drop_table("run_triage")
    op.drop_table("triage_chats")
    op.drop_column("projects", "triage_enabled")
