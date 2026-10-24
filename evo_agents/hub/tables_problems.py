"""The tables of the problems of a project (0134, plan problem-oriented-hub), which ``evo_agents.hub.tables`` adds to
its metadata and exposes as its own attributes (``tables.problems``, ``tables.problem_revisions``): queries name them
there.

problems holds one row per problem of a project (``evo_agents.hub.problems``): its number (the code P-<number>, unique
in the project and never given again, since a problem is closed and never deleted) and its newest revision. A
project's problems go with the project.

problem_revisions holds every revision of each problem, never changed: the statement, who is hurt, when it happens,
how they cope today, the metric (an object, or NULL), the rank, the owner, the status with the reason of a closed one,
the writer's note, who wrote it and when. A problem is its newest revision (``problems.revision``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def problem_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(problems, problem_revisions), added to ``metadata``."""
    problems = Table(
        "problems",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("revision", Integer, nullable=False),
        _stamp("created_at"),
        _key("problems", "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="problems_pkey"),
        UniqueConstraint("project_id", "number", name="problems_number_key"),
    )
    name = "problem_revisions"
    revisions = Table(
        name,
        metadata,
        Column("problem_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("statement", Text, nullable=False),
        Column("who", Text, nullable=False),
        Column("when", Text, nullable=False),
        Column("workaround", Text, nullable=False),
        Column("metric", JSONB(none_as_null=True)),  # None is SQL NULL: no metric
        Column("rank", Integer, nullable=False),
        Column("owner_id", BigInteger, nullable=False),
        Column("status", Text, nullable=False),
        Column("close_reason", Text),
        Column("note", Text),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        _key(name, "problem_id", "problems", ondelete="CASCADE"),
        _key(name, "owner_id", "users", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("problem_id", "revision", name="problem_revisions_pkey"),
    )
    return problems, revisions


def evidence_table(metadata: MetaData) -> Table:
    """problem_evidence (0137, plan problem-intake), added to ``metadata``: one row per line of evidence of a problem,
    numbered within the problem (the code E-<number>). A line names the kind and address of its source, where in it,
    who spoke, an excerpt of at most 300 characters, the signal it shows and its label (public or internal), who added
    it, who confirmed it and when, and the proposal of @builder that made it, if one did. A problem's lines go with it."""
    name = "problem_evidence"
    table = Table(
        name,
        metadata,
        Column("problem_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("source_kind", Text, nullable=False),
        Column("source_uri", Text, nullable=False),
        Column("locator", Text),
        Column("speaker", Text),
        Column("excerpt", Text, nullable=False),
        Column("signal", Text, nullable=False),
        Column("label", Text, nullable=False),
        Column("added_by", BigInteger, nullable=False),
        _stamp("created_at"),
        Column("confirmed_by", BigInteger),
        Column("confirmed_at", DateTime(timezone=True)),
        Column("proposal_id", BigInteger),
        _key(name, "problem_id", "problems", ondelete="CASCADE"),
        _key(name, "added_by", "users", ondelete="RESTRICT"),
        _key(name, "confirmed_by", "users", ondelete="RESTRICT"),
        _key(name, "proposal_id", "builder_proposals", ondelete="SET NULL"),
        PrimaryKeyConstraint("problem_id", "number", name="problem_evidence_pkey"),
    )
    Index(f"{name}_proposal_idx", table.c.proposal_id, postgresql_where=table.c.proposal_id.is_not(None))
    return table
