"""The table of the threads of an agent system's Orchestrator (0221), which ``evo_agents.hub.tables`` adds to its
metadata and exposes as its own attribute (``tables.orchestrator_threads``): queries name it there.

orchestrator_threads holds each thread the hub opened to the Orchestrator of a system in the system's ops chat
(``agent_systems.ops_chat_id``; ``evo_agents.hub.server.orchestrator``): the system and project, the trace when there is
one, the agent it is about (``subject``) and its run, the rule that opened it (``triage_rules.ORCHESTRATOR_RULES``) with
the ids of the spans that show it (``evidence``), the thread (chat and first message), the Orchestrator's run, the wait
of the run that asked (``wait_id``, of ``asker_run_id``), which the Orchestrator's answer or the agent it gives the work
to answers, the hop of the work (an escalation adds none, and work given to another agent keeps it), and how it ended
(``outcome``, ``closed_at``). ``key`` makes a rule open one thread: one for each failed or stale run, each turn out of
rounds, each idle or over-budget trace, each agent in a loop of a trace; an escalation has none.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

# triage_rules.ORCHESTRATOR_RULES and ORCHESTRATOR_ACTIONS; a test keeps them equal
RULES = ("failed", "stale", "escalate", "loop", "idle", "rounds", "budget", "ops", "draft")  # ops: 0241, draft: 0242
OUTCOMES = ("answer", "reassign", "ask_owner")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(value) for value in values)})"


def orchestrator_tables(metadata: MetaData) -> Table:
    """orchestrator_threads, added to ``metadata``."""
    name = "orchestrator_threads"
    threads = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("system_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("trace_id", BigInteger),
        Column("subject", Text, nullable=False),
        Column("subject_run_id", BigInteger),
        Column("rule", Text, nullable=False),
        Column("key", Text),
        Column("evidence", JSONB, nullable=False, server_default="[]"),
        Column("chat_id", BigInteger),
        Column("root_message_id", BigInteger),
        Column("orchestrator_run_id", BigInteger),
        Column("asker_run_id", BigInteger),
        Column("wait_id", BigInteger),
        Column("hop", Integer, nullable=False, server_default="1"),
        Column("outcome", Text),
        Column("closed_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["system_id"], ["agent_systems.id"], ondelete="CASCADE", name=f"{name}_system_id_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["trace_id"], ["system_traces.id"], ondelete="SET NULL", name=f"{name}_trace_id_fkey"),
        ForeignKeyConstraint(["subject_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{name}_subject_run_id_fkey"),
        ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="SET NULL", name=f"{name}_chat_id_fkey"),
        ForeignKeyConstraint(
            ["root_message_id"], ["chat_messages.id"], ondelete="SET NULL", name=f"{name}_root_message_id_fkey"
        ),
        ForeignKeyConstraint(
            ["orchestrator_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{name}_orchestrator_run_id_fkey"
        ),
        ForeignKeyConstraint(["asker_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{name}_asker_run_id_fkey"),
        ForeignKeyConstraint(["wait_id"], ["decisions.id"], ondelete="SET NULL", name=f"{name}_wait_id_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        CheckConstraint(_in("rule", RULES), name=f"{name}_rule_check"),
        CheckConstraint(f"outcome is null or {_in('outcome', OUTCOMES)}", name=f"{name}_outcome_check"),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name=f"{name}_evidence_check"),
    )
    Index(f"{name}_key", threads.c.key, unique=True, postgresql_where=threads.c.key.is_not(None))
    Index(f"{name}_thread_idx", threads.c.chat_id, threads.c.root_message_id)
    Index(f"{name}_system_idx", threads.c.system_id, threads.c.id.desc())
    return threads
