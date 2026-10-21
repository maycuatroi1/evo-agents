"""The table of the core plugin pm (0086), which ``evo_agents.hub.tables`` adds to its metadata as ``tables.pm_sends``.

pm_sends holds one row for each thing pm sent a project on a day: its digest (kind ``digest``, ``key`` empty) or the
escalation of one blocker (kind ``escalation``, ``key`` names the blocker). The unique key (project, day, kind, key)
is what keeps the job from sending twice. The CHECK on ``kind`` is the migration's alone.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
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

KINDS = ("digest", "escalation")


def pm_tables(metadata: MetaData) -> Table:
    """pm_sends (0086), added to ``metadata``."""
    return Table(
        "pm_sends",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("day", Date, nullable=False),
        Column("kind", Text, nullable=False),
        Column("key", Text, nullable=False, server_default=""),
        Column("sent_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="pm_sends_project_id_fkey"),
        PrimaryKeyConstraint("id", name="pm_sends_pkey"),
        UniqueConstraint("project_id", "day", "kind", "key", name="pm_sends_once_key"),
    )
