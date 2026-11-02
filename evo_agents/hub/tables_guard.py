"""The tables of the core plugin guard (0192), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its
own attributes (``tables.guard_terms``, ``tables.guard_targets``): queries name them there.

guard_terms holds the private names of a project, which no public repo may carry: the term as an admin wrote it, its
words as ``evo_agents.private_names`` reads them (unique in the project), a note and who added it. guard_targets holds
the repos a project publishes, as ``host/owner/repo`` patterns (unique in the project). The hub hands every worker and
member the union of both over the projects that enable the plugin, the terms hashed (``evo_agents.core_plugins.guard``).

Their CHECK constraints are the migration's alone.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)


def _list(metadata: MetaData, name: str, value: str) -> Table:
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column(value, Text, nullable=False),
        *([Column("words", Text, nullable=False)] if value == "term" else []),
        Column("note", Text, nullable=True),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name=f"{name}_created_by_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        UniqueConstraint("project_id", "words" if value == "term" else value, name=f"{name}_key"),
    )


def guard_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(guard_terms, guard_targets), added to ``metadata``."""
    return _list(metadata, "guard_terms", "term"), _list(metadata, "guard_targets", "pattern")
