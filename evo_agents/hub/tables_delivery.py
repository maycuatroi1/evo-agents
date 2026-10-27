"""The ledger of what is delivered of an SRS document (0172, plan srs-backlog-loop), which ``evo_agents.hub.tables``
adds to its metadata as ``tables.srs_delivery``: queries name it there. The model is ``evo_agents.hub.srs_backlog``.

srs_delivery holds one row per use case of a document the hub knows a state of: the document, the use case's code (or
its key when it has none) as written and as compared (``code_key``), its state (delivered, undelivered, unknown,
delivering), who put it there (kg: the hub from what the knowledge graph shows; admin: a person; deliver: the Loop when
a deliver finished or began), the evidence (an object: the plan, the merge, the commit, the pull request, the code or
the person that told it), the goal and the work item of the Loop it came of, the member who wrote it, and when it was
made and changed. A use case with no row is as an undelivered one for the backlog. Only the hub writes it.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    column,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

STATES = ("delivered", "undelivered", "unknown", "delivering")  # srs_backlog.STATES
SOURCES = ("kg", "admin", "deliver")  # srs_backlog.SOURCES


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def delivery_table(metadata: MetaData) -> Table:
    """srs_delivery, added to ``metadata``."""
    name = "srs_delivery"
    table = Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("document_id", BigInteger, nullable=False),
        Column("code_key", Text, nullable=False),
        Column("code", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("source", Text, nullable=False),
        Column("evidence", JSONB, nullable=False, server_default="{}"),
        Column("goal_id", BigInteger),
        Column("item_id", BigInteger),
        Column("updated_by", BigInteger),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(name, "goal_id", "goals", ondelete="SET NULL"),
        _key(name, "item_id", "loop_items", ondelete="SET NULL"),
        _key(name, "updated_by", "users", ondelete="SET NULL"),
        CheckConstraint(column("state").in_(STATES), name=f"{name}_state_check"),
        CheckConstraint(column("source").in_(SOURCES), name=f"{name}_source_check"),
        PrimaryKeyConstraint("document_id", "code_key", name="srs_delivery_pkey"),
    )
    Index("srs_delivery_project_idx", table.c.project_id, table.c.state)
    return table


def baseline_table(metadata: MetaData) -> Table:
    """srs_baselines (0173), added to ``metadata``: the goals whose baseline an admin confirmed. One row per goal: the
    document the baseline is of (a goal re-aimed at another document is unconfirmed again), who confirmed and when,
    and the use cases of each state the ledger held then (``counts``)."""
    name = "srs_baselines"
    return Table(
        name,
        metadata,
        Column("goal_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("document_id", BigInteger, nullable=False),
        Column("confirmed_by", BigInteger),
        Column("counts", JSONB, nullable=False, server_default="{}"),
        _stamp("confirmed_at"),
        _key(name, "goal_id", "goals", ondelete="CASCADE"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(name, "confirmed_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("goal_id", name="srs_baselines_pkey"),
    )
