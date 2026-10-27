"""The ledger of confirmed answers a run's prompt carried (0175, plan active-context-answers), which
``evo_agents.hub.tables`` adds to its metadata as ``tables.context_answer_loads``: queries name it there.

One row per answer loaded into a run: the run and its project, the source (``decision`` or ``memory``), the source's id,
the scope key that matched it (``repo:REPO``, ``dir:REPO:DIR`` or ``path:REPO:PATH``, the strongest one) and when. A run
loads an answer once (``context_answer_loads_key``), so a run claimed again adds nothing. The rows give the share of
runs that loaded an answer and the answers loaded a day or a week (``evo_agents.hub.server.context_answers``). Only the
hub writes them.
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
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    column,
    func,
)

SOURCES = ("decision", "memory")  # context_answers.SOURCES


def loads_table(metadata: MetaData) -> Table:
    name = "context_answer_loads"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("run_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("source", Text, nullable=False),
        Column("source_id", BigInteger, nullable=False),
        Column("scope_key", Text, nullable=False),
        Column("loaded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name=f"{name}_run_id_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        UniqueConstraint("run_id", "source", "source_id", name=f"{name}_key"),
        CheckConstraint(column("source").in_(SOURCES), name=f"{name}_source_check"),
        Index(f"{name}_project_idx", "project_id", "loaded_at"),
    )
