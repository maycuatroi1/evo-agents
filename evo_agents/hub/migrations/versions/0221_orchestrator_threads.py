"""The threads of a system's Orchestrator and the ops chat they are in: orchestrator_threads, agent_systems.ops_chat_id.

Revision ID: 0221
Revises: 0220
Create Date: 2026-11-07

When an agent that its system's Orchestrator orchestrates fails, goes stale, escalates, or trips a rule of progress
(a loop, an idle trace, a turn out of rounds, a trace out of budget), the hub opens a thread to the Orchestrator in
the system's ops chat (plan multi-agent-systems, step 5; ``evo_agents.hub.server.orchestrator``). The thread row keeps
the rule, the evidence spans, the wait of the run that asked and how the Orchestrator ended it. The rule and outcome
lists below are copies of ``evo_agents.hub.tables_orchestrator`` as of this revision, never imports.

Going back drops the table and the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0221"
down_revision = "0220"
branch_labels = None
depends_on = None

THREADS = "orchestrator_threads"
SYSTEMS = "agent_systems"
RULES = ("failed", "stale", "escalate", "loop", "idle", "rounds", "budget")
OUTCOMES = ("answer", "reassign", "ask_owner")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(value) for value in values)})"


def _fk(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete=ondelete, name=f"{THREADS}_{column}_fkey")


def upgrade() -> None:
    op.add_column(SYSTEMS, sa.Column("ops_chat_id", sa.BigInteger()))
    op.create_foreign_key(f"{SYSTEMS}_ops_chat_id_fkey", SYSTEMS, "chats", ["ops_chat_id"], ["id"], ondelete="SET NULL")
    op.create_table(
        THREADS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("system_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.BigInteger()),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("subject_run_id", sa.BigInteger()),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("key", sa.Text()),
        sa.Column("evidence", JSONB(), nullable=False, server_default="[]"),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("root_message_id", sa.BigInteger()),
        sa.Column("orchestrator_run_id", sa.BigInteger()),
        sa.Column("asker_run_id", sa.BigInteger()),
        sa.Column("wait_id", sa.BigInteger()),
        sa.Column("hop", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("outcome", sa.Text()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        _fk("system_id", "agent_systems.id", ondelete="CASCADE"),
        _fk("project_id", "projects.id", ondelete="CASCADE"),
        _fk("trace_id", "system_traces.id", ondelete="SET NULL"),
        _fk("subject_run_id", "runs.id", ondelete="SET NULL"),
        _fk("chat_id", "chats.id", ondelete="SET NULL"),
        _fk("root_message_id", "chat_messages.id", ondelete="SET NULL"),
        _fk("orchestrator_run_id", "runs.id", ondelete="SET NULL"),
        _fk("asker_run_id", "runs.id", ondelete="SET NULL"),
        _fk("wait_id", "decisions.id", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{THREADS}_pkey"),
        sa.CheckConstraint(_in("rule", RULES), name=f"{THREADS}_rule_check"),
        sa.CheckConstraint(f"outcome is null or {_in('outcome', OUTCOMES)}", name=f"{THREADS}_outcome_check"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'array'", name=f"{THREADS}_evidence_check"),
    )
    op.create_index(f"{THREADS}_key", THREADS, ["key"], unique=True, postgresql_where=sa.column("key").is_not(None))
    op.create_index(f"{THREADS}_thread_idx", THREADS, ["chat_id", "root_message_id"])
    op.create_index(f"{THREADS}_system_idx", THREADS, [sa.column("system_id"), sa.column("id").desc()])


def downgrade() -> None:
    op.drop_index(f"{THREADS}_system_idx", table_name=THREADS)
    op.drop_index(f"{THREADS}_thread_idx", table_name=THREADS)
    op.drop_index(f"{THREADS}_key", table_name=THREADS)
    op.drop_table(THREADS)
    op.drop_constraint(f"{SYSTEMS}_ops_chat_id_fkey", SYSTEMS, type_="foreignkey")
    op.drop_column(SYSTEMS, "ops_chat_id")
