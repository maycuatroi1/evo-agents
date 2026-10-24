"""The table of the intakes of a project (0138, plan problem-intake), which ``evo_agents.hub.tables`` adds to its
metadata and exposes as its own attribute (``tables.intakes``): queries name it there.

intakes holds one row per intake of a project (``evo_agents.hub.intakes``): its number (the code I-<number>, unique in
the project), the member who asked for it, the list of sources the session read (kind, label and a short name of each,
never raw content), its status (open, done), and once the session put its result: the result as it came
(``evo-intake/1``), what the hub made of it (proposals, tasks), who put it and when. A project's intakes go with it.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def intake_table(metadata: MetaData) -> Table:
    """intakes (0138), added to ``metadata``."""
    name = "intakes"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("requested_by", BigInteger, nullable=False),
        Column("sources", JSONB, nullable=False),
        Column("note", Text),
        Column("status", Text, nullable=False, server_default="open"),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("result", JSONB),
        Column("outcome", JSONB),
        Column("put_by", BigInteger),
        Column("done_at", DateTime(timezone=True)),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "requested_by", "users", ondelete="RESTRICT"),
        _key(name, "put_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="intakes_pkey"),
        UniqueConstraint("project_id", "number", name="intakes_number_key"),
    )
