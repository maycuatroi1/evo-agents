"""Traces of agent systems and the briefs of asks: system_traces, system_spans, agent_ask_briefs.

Revision ID: 0218
Revises: 0217
Create Date: 2026-11-07

A person's message to the entry agent of an agent system opens a trace (plan multi-agent-systems, step 3,
``evo_agents.hub.tables_traces``); each run of it, and each ask, mention, forward and query of a knowledge graph in it,
is a span. A partial unique index keeps one invoke_agent span a run. An ask with a brief keeps it for the run it made.

Going back drops the three tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0218"
down_revision = "0217"
branch_labels = None
depends_on = None

TRACES = "system_traces"
SPANS = "system_spans"
BRIEFS = "agent_ask_briefs"
SPAN_NAMES = ("invoke_agent", "execute_tool")
SPAN_STATUSES = ("running", "ok", "error", "refused")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        TRACES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("system_id", sa.BigInteger()),
        sa.Column("system_slug", sa.Text(), nullable=False),
        sa.Column("system_revision", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("opened_by", sa.BigInteger()),
        _stamp("started_at"),
        _stamp("last_at"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{TRACES}_project_id_fkey"),
        sa.ForeignKeyConstraint(
            ["system_id"], ["agent_systems.id"], ondelete="SET NULL", name=f"{TRACES}_system_id_fkey"
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="SET NULL", name=f"{TRACES}_chat_id_fkey"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"], ondelete="SET NULL", name=f"{TRACES}_message_id_fkey"
        ),
        sa.ForeignKeyConstraint(["opened_by"], ["users.id"], ondelete="SET NULL", name=f"{TRACES}_opened_by_fkey"),
        sa.PrimaryKeyConstraint("id", name=f"{TRACES}_pkey"),
    )
    op.create_index(
        f"{TRACES}_system_idx", TRACES, [sa.column("project_id"), sa.column("system_slug"), sa.column("id").desc()]
    )
    op.create_index(f"{TRACES}_chat_idx", TRACES, [sa.column("chat_id"), sa.column("id").desc()])
    op.create_table(
        SPANS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("trace_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_id", sa.BigInteger()),
        sa.Column("run_id", sa.BigInteger()),
        sa.Column("project_id", sa.BigInteger()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text()),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("node", sa.Text()),
        sa.Column("from_node", sa.Text()),
        sa.Column("to_node", sa.Text()),
        sa.Column("runtime", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("off_design", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text()),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("attributes", JSONB(), nullable=False, server_default="{}"),
        _stamp("started_at"),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["trace_id"], [f"{TRACES}.id"], ondelete="CASCADE", name=f"{SPANS}_trace_id_fkey"),
        sa.ForeignKeyConstraint(["parent_id"], [f"{SPANS}.id"], ondelete="SET NULL", name=f"{SPANS}_parent_id_fkey"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name=f"{SPANS}_run_id_fkey"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL", name=f"{SPANS}_project_id_fkey"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"], ondelete="SET NULL", name=f"{SPANS}_message_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name=f"{SPANS}_pkey"),
        sa.CheckConstraint(_in("name", SPAN_NAMES), name=f"{SPANS}_name_check"),
        sa.CheckConstraint(_in("status", SPAN_STATUSES), name=f"{SPANS}_status_check"),
        sa.CheckConstraint("jsonb_typeof(attributes) = 'object'", name=f"{SPANS}_attributes_check"),
    )
    op.create_index(f"{SPANS}_trace_idx", SPANS, ["trace_id", "id"])
    op.create_index(
        f"{SPANS}_run_key",
        SPANS,
        ["run_id"],
        unique=True,
        postgresql_where=(sa.column("name") == "invoke_agent") & sa.column("run_id").is_not(None),
    )
    op.create_table(
        BRIEFS,
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("asked_by_run_id", sa.BigInteger()),
        sa.Column("brief", JSONB(), nullable=False),
        _stamp("created_at"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name=f"{BRIEFS}_run_id_fkey"),
        sa.ForeignKeyConstraint(["asked_by_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{BRIEFS}_asked_by_fkey"),
        sa.PrimaryKeyConstraint("run_id", name=f"{BRIEFS}_pkey"),
        sa.CheckConstraint("jsonb_typeof(brief) = 'object'", name=f"{BRIEFS}_brief_check"),
    )


def downgrade() -> None:
    op.drop_table(BRIEFS)
    op.drop_index(f"{SPANS}_run_key", table_name=SPANS)
    op.drop_index(f"{SPANS}_trace_idx", table_name=SPANS)
    op.drop_table(SPANS)
    op.drop_index(f"{TRACES}_chat_idx", table_name=TRACES)
    op.drop_index(f"{TRACES}_system_idx", table_name=TRACES)
    op.drop_table(TRACES)
