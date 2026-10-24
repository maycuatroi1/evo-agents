"""The tables of the opportunities of a project (0141, plan opportunity-intake), which ``evo_agents.hub.tables`` adds to
its metadata and exposes as its own attributes (``tables.opportunities``, ``tables.opportunity_revisions``,
``tables.opportunity_answers``, ``tables.opportunity_evidence``): queries name them there.

opportunities holds one row per opportunity of a project (``evo_agents.hub.opportunities``): its number (the code
O-<number>, unique in the project and never given again, since an opportunity is closed and never deleted) and its
newest revision. A project's opportunities go with the project.

opportunity_revisions holds every revision of each opportunity, never changed: the statement, who benefits, the
leverage, the window (a date and a note, both optional), the metric (an object, or NULL), the rank, the owner, the
status with the reason of a closed one, the writer's note, who wrote it and when.

opportunity_answers holds, for each revision, the problems it answers in order. A row names the problem itself, not its
code, so a project that is renamed keeps its references; the problem may belong to another project than the
opportunity's, which is the only place where two projects' rows meet.

opportunity_evidence holds the lines of evidence of an opportunity, as problem_evidence holds a problem's.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
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


def opportunity_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(opportunities, opportunity_revisions, opportunity_answers), added to ``metadata``."""
    opportunities = Table(
        "opportunities",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("revision", Integer, nullable=False),
        _stamp("created_at"),
        _key("opportunities", "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="opportunities_pkey"),
        UniqueConstraint("project_id", "number", name="opportunities_number_key"),
    )
    name = "opportunity_revisions"
    revisions = Table(
        name,
        metadata,
        Column("opportunity_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("statement", Text, nullable=False),
        Column("beneficiary", Text, nullable=False),
        Column("leverage", Text, nullable=False),
        Column("window_until", Date),
        Column("window_note", Text),
        Column("metric", JSONB(none_as_null=True)),  # None is SQL NULL: no metric
        Column("rank", Integer, nullable=False),
        Column("owner_id", BigInteger, nullable=False),
        Column("status", Text, nullable=False),
        Column("close_reason", Text),
        Column("note", Text),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        _key(name, "opportunity_id", "opportunities", ondelete="CASCADE"),
        _key(name, "owner_id", "users", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("opportunity_id", "revision", name="opportunity_revisions_pkey"),
    )
    name = "opportunity_answers"
    answers = Table(
        name,
        metadata,
        Column("opportunity_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("position", Integer, nullable=False),
        Column("problem_id", BigInteger, nullable=False),
        ForeignKeyConstraint(
            ["opportunity_id", "revision"],
            ["opportunity_revisions.opportunity_id", "opportunity_revisions.revision"],
            ondelete="CASCADE",
            name=f"{name}_revision_fkey",
        ),
        _key(name, "problem_id", "problems", ondelete="CASCADE"),
        PrimaryKeyConstraint("opportunity_id", "revision", "position", name=f"{name}_pkey"),
        UniqueConstraint("opportunity_id", "revision", "problem_id", name=f"{name}_problem_key"),
    )
    Index(f"{name}_problem_idx", answers.c.problem_id)
    return opportunities, revisions, answers


def evidence_table(metadata: MetaData) -> Table:
    """opportunity_evidence (0141, plan opportunity-intake), added to ``metadata``: one row per line of evidence of an
    opportunity, numbered within it (the code E-<number>). A line names the kind and address of its source, where in
    it, who spoke, an excerpt of at most 300 characters, the signal it shows and its label (public or internal), who
    added it, who confirmed it and when, and the proposal of @builder that made it, if one did. An opportunity's lines
    go with it."""
    name = "opportunity_evidence"
    table = Table(
        name,
        metadata,
        Column("opportunity_id", BigInteger, nullable=False),
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
        _key(name, "opportunity_id", "opportunities", ondelete="CASCADE"),
        _key(name, "added_by", "users", ondelete="RESTRICT"),
        _key(name, "confirmed_by", "users", ondelete="RESTRICT"),
        _key(name, "proposal_id", "builder_proposals", ondelete="SET NULL"),
        PrimaryKeyConstraint("opportunity_id", "number", name=f"{name}_pkey"),
    )
    Index(f"{name}_proposal_idx", table.c.proposal_id, postgresql_where=table.c.proposal_id.is_not(None))
    return table
