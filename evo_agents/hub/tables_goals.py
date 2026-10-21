"""The tables of the goals of a project (0091, plan loop-engine), which ``evo_agents.hub.tables`` adds to its metadata
and exposes as its own attributes (``tables.goals``, ``tables.goal_revisions``): queries name them there.

goals holds one row per goal of a project (``evo_agents.hub.goals``): its number (the code G-<number>, unique in the
project and never given again, since a goal is closed and never deleted), the alias it keeps when the hub made it from
a goal of the Curator's charter (the charter's id, unique in the project), and its newest revision. A project's goals
go with the project.

goal_revisions holds every revision of each goal, never changed: the statement, the metric (an object, or NULL), the
deadline, the canvas it drives, the rank, the owner, the status with the reason of a closed one, the writer's note, who
wrote it and when. A goal is its newest revision (``goals.revision``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
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


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def goal_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(goals, goal_revisions), added to ``metadata``."""
    goals = Table(
        "goals",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("alias", Text),
        Column("revision", Integer, nullable=False),
        _stamp("created_at"),
        _key("goals", "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="goals_pkey"),
        UniqueConstraint("project_id", "number", name="goals_number_key"),
        UniqueConstraint("project_id", "alias", name="goals_alias_key"),
    )
    name = "goal_revisions"
    revisions = Table(
        name,
        metadata,
        Column("goal_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("statement", Text, nullable=False),
        Column("metric", JSONB(none_as_null=True)),  # None is SQL NULL: no metric
        Column("deadline", Date),
        Column("canvas_id", BigInteger),
        Column("rank", Integer, nullable=False),
        Column("owner_id", BigInteger, nullable=False),
        Column("status", Text, nullable=False),
        Column("close_reason", Text),
        Column("note", Text),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        _key(name, "goal_id", "goals", ondelete="CASCADE"),
        _key(name, "canvas_id", "canvas_canvases", ondelete="RESTRICT"),
        _key(name, "owner_id", "users", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("goal_id", "revision", name="goal_revisions_pkey"),
    )
    return goals, revisions
