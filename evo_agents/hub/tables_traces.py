"""The tables of the traces of agent systems (0218), which ``evo_agents.hub.tables`` adds to its metadata and exposes
as its own attributes (``tables.system_traces``, ``tables.system_spans``, ``tables.agent_ask_briefs``): queries name
them there.

system_traces holds each trace of an agent system (``evo_agents.hub.server.system_traces``): the system and the
revision of its spec it ran under, the chat and the message of a person that opened it, who wrote that message, and
when it started and last moved. system_spans holds its spans, named as OpenTelemetry GenAI names them
(``system_context.SPAN_NAMES``): one ``invoke_agent`` span a run (a partial unique index holds it, and maps a run to
its trace), and ``execute_tool`` spans for an ask, a mention, a forward or a query of a knowledge graph. A span names
the agent, the node of the spec it is (``@handle`` for an agent the spec lacks), the node it came from, whether that
edge is off the design and why, its runtime, status and times; content stays where it is (the message, the run) and a
span points at it. Tokens and cost are read from run_spend when a trace is shown. agent_ask_briefs holds the brief an
ask gave the run it made (goal, output, bounds, done_when, refs), which heads that run's prompt.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

SPAN_NAMES = ("invoke_agent", "execute_tool")  # system_context.SPAN_NAMES; a test keeps the two equal
SPAN_STATUSES = ("running", "ok", "error", "refused")  # system_context.SPAN_STATUSES


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(value) for value in values)})"


def trace_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(system_traces, system_spans, agent_ask_briefs), added to ``metadata``."""
    name = "system_traces"
    traces = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("system_id", BigInteger),
        Column("system_slug", Text, nullable=False),
        Column("system_revision", Integer, nullable=False),
        Column("chat_id", BigInteger),
        Column("message_id", BigInteger),
        Column("opened_by", BigInteger),
        Column("work_chat_id", BigInteger),  # 0234: the work chat its first hand-off opened (``work_chats``)
        _stamp("started_at"),
        _stamp("last_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["system_id"], ["agent_systems.id"], ondelete="SET NULL", name=f"{name}_system_id_fkey"),
        ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="SET NULL", name=f"{name}_chat_id_fkey"),
        ForeignKeyConstraint(["message_id"], ["chat_messages.id"], ondelete="SET NULL", name=f"{name}_message_id_fkey"),
        ForeignKeyConstraint(["opened_by"], ["users.id"], ondelete="SET NULL", name=f"{name}_opened_by_fkey"),
        ForeignKeyConstraint(["work_chat_id"], ["chats.id"], ondelete="SET NULL", name=f"{name}_work_chat_id_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
    )
    Index(f"{name}_system_idx", traces.c.project_id, traces.c.system_slug, traces.c.id.desc())
    Index(f"{name}_chat_idx", traces.c.chat_id, traces.c.id.desc())
    sname = "system_spans"
    spans = Table(
        sname,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("trace_id", BigInteger, nullable=False),
        Column("parent_id", BigInteger),
        Column("run_id", BigInteger),
        Column("project_id", BigInteger),
        Column("name", Text, nullable=False),
        Column("tool", Text),
        Column("agent", Text, nullable=False),
        Column("node", Text),
        Column("from_node", Text),
        Column("to_node", Text),
        Column("runtime", Text),
        Column("status", Text, nullable=False, server_default="ok"),
        Column("off_design", Boolean, nullable=False, server_default=false()),
        Column("reason", Text),
        Column("message_id", BigInteger),
        Column("attributes", JSONB, nullable=False, server_default="{}"),
        _stamp("started_at"),
        Column("ended_at", DateTime(timezone=True)),
        ForeignKeyConstraint(["trace_id"], ["system_traces.id"], ondelete="CASCADE", name=f"{sname}_trace_id_fkey"),
        ForeignKeyConstraint(["parent_id"], ["system_spans.id"], ondelete="SET NULL", name=f"{sname}_parent_id_fkey"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name=f"{sname}_run_id_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL", name=f"{sname}_project_id_fkey"),
        ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"], ondelete="SET NULL", name=f"{sname}_message_id_fkey"
        ),
        PrimaryKeyConstraint("id", name=f"{sname}_pkey"),
        CheckConstraint(_in("name", SPAN_NAMES), name=f"{sname}_name_check"),
        CheckConstraint(_in("status", SPAN_STATUSES), name=f"{sname}_status_check"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name=f"{sname}_attributes_check"),
    )
    Index(f"{sname}_trace_idx", spans.c.trace_id, spans.c.id)
    Index(  # one invoke_agent span a run: the run's place in its trace
        f"{sname}_run_key",
        spans.c.run_id,
        unique=True,
        postgresql_where=(spans.c.name == "invoke_agent") & spans.c.run_id.is_not(None),
    )
    bname = "agent_ask_briefs"
    briefs = Table(
        bname,
        metadata,
        Column("run_id", BigInteger, nullable=False),
        Column("asked_by_run_id", BigInteger),
        Column("brief", JSONB, nullable=False),
        _stamp("created_at"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name=f"{bname}_run_id_fkey"),
        ForeignKeyConstraint(["asked_by_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{bname}_asked_by_fkey"),
        PrimaryKeyConstraint("run_id", name=f"{bname}_pkey"),
        CheckConstraint("jsonb_typeof(brief) = 'object'", name=f"{bname}_brief_check"),
    )
    return traces, spans, briefs
