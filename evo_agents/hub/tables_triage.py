"""The tables of the triage of failed runs (0039), which ``evo_agents.hub.tables`` adds to its metadata and exposes as
its own attributes (``tables.triage_chats``, ``tables.run_triage``): queries name them there.

triage_chats holds the group chats the hub opened for the triage of failed runs: the project's (``plan_id`` NULL), one
per project, which the failed runs of all its plans and its runs with no plan open their threads in; and one per plan
for a plan kept apart from it (``evo_agents.hub.server.triage``: a label not at or below the project's default label, or
a run whose owner the project's chat cannot take), the rows each plan had before staying as they were. run_triage holds
the thread of each failed run: its chat and the first message of the thread, the cause it failed of and whether the hub
guessed it, the chat run its mention made, and how far a chain of reruns went. A run has one row at most, which is what
makes the hub open one thread for a run however often it handles its failure. ``projects.triage_enabled`` is the switch
of a project: off, a failed run sends its owner the notice ``run_failed`` and opens no thread.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    true,
)


def _fk(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [target], ondelete=ondelete, name=f"{table}_{column}_fkey")


def triage_tables(metadata: MetaData, projects: Table, runs: Table) -> tuple[Table, Table]:
    """(triage_chats, run_triage), added to ``metadata``; the switch of a project added to ``projects``."""
    del runs  # its keys are named by table and column, like the other tables' of this package
    projects.append_column(Column("triage_enabled", Boolean, nullable=False, server_default=true()))
    chats = Table(
        "triage_chats",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("plan_id", Text),
        Column("chat_id", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _fk("triage_chats", "project_id", "projects.id", ondelete="CASCADE"),
        _fk("triage_chats", "chat_id", "chats.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="triage_chats_pkey"),
        UniqueConstraint("chat_id", name="triage_chats_chat_id_key"),
        UniqueConstraint("project_id", "plan_id", name="triage_chats_plan_key", postgresql_nulls_not_distinct=True),
    )
    triage = Table(
        "run_triage",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("run_id", BigInteger, nullable=False),
        Column("chat_id", BigInteger, nullable=False),
        Column("root_message_id", BigInteger, nullable=False),
        Column("cause", Text),
        Column("cause_inferred", Boolean, nullable=False, server_default="false"),
        Column("agent_run_id", BigInteger),
        Column("chain_depth", SmallInteger, nullable=False, server_default="0"),
        Column("auto_reruns", SmallInteger, nullable=False, server_default="0"),
        Column("opened_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _fk("run_triage", "run_id", "runs.id", ondelete="CASCADE"),
        _fk("run_triage", "chat_id", "chats.id", ondelete="CASCADE"),
        _fk("run_triage", "root_message_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("run_triage", "agent_run_id", "runs.id", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="run_triage_pkey"),
        UniqueConstraint("run_id", name="run_triage_run_id_key"),
    )
    Index("run_triage_chat_idx", triage.c.chat_id)
    return chats, triage
