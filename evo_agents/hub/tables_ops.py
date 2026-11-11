"""The tables of a project's queue of operations events (0240, plan hub-steward), which ``evo_agents.hub.tables`` adds
to its metadata and exposes as its own attributes (``tables.ops_cursors``, ``tables.ops_batches``,
``tables.ops_items``): queries name them there. ``evo_agents.hub.ops_queue`` describes the model.

- ops_cursors: one row per project whose queue ever read: the id of the last row of hub_events it read, as loop_states
  holds the Loop's.
- ops_batches: the items of one cause key (kind, cause, subject) of a project, gathered until ``closes_at``: ``open``
  while it gathers (at most one per key, ``ops_batches_open_key``), then ``ready`` when it closed with items left, or
  ``empty`` when every item left before; ``running`` once the hub opened its ops thread (``thread_id``, 0241) to the
  Orchestrator of the default system, whose run it keeps, and ``done`` when that run ended; ``recurring`` when its
  cause key came back often (``server.ops_threads``).
- ops_items: each operations event the queue took into a batch, with the ids it names, ``pending`` until tier 0
  handled it, ``handled`` then (by what, when); one per event (``ops_items_event_key``).
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
    Table,
    Text,
    false,
    func,
    text,
)


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _cursors(metadata: MetaData) -> Table:
    name = "ops_cursors"
    return Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("cursor", BigInteger, nullable=False, server_default="0"),
        _stamp("updated_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("project_id", name="ops_cursors_pkey"),
    )


def _batches(metadata: MetaData) -> Table:
    name = "ops_batches"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("key", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("cause", Text, nullable=False),
        Column("subject", Text, nullable=False),
        Column("state", Text, nullable=False, server_default="open"),
        _stamp("opened_at"),
        Column("closes_at", DateTime(timezone=True), nullable=False),
        _when("closed_at"),
        Column("run_id", BigInteger),
        Column("thread_id", BigInteger),  # the ops thread opened for it (0241)
        Column("recurring", Boolean, nullable=False, server_default=false()),  # its cause key came back often (0241)
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "thread_id", "orchestrator_threads", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="ops_batches_pkey"),
    )
    Index("ops_batches_open_key", table.c.project_id, table.c.key, unique=True, postgresql_where=text("state = 'open'"))
    Index("ops_batches_project_idx", table.c.project_id, table.c.id)
    return table


def _items(metadata: MetaData) -> Table:
    name = "ops_items"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("batch_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("event_id", BigInteger),
        Column("kind", Text, nullable=False),
        Column("cause", Text, nullable=False),
        Column("subject", Text, nullable=False),
        Column("run_id", BigInteger),
        Column("decision_id", BigInteger),
        Column("worker_id", BigInteger),
        Column("state", Text, nullable=False, server_default="pending"),
        Column("handled_by", Text),
        _stamp("created_at"),
        _when("handled_at"),
        _key(name, "batch_id", "ops_batches", ondelete="CASCADE"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "event_id", "hub_events", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="ops_items_pkey"),
    )
    Index("ops_items_event_key", table.c.event_id, unique=True)
    Index("ops_items_batch_idx", table.c.batch_id)
    Index("ops_items_pending_idx", table.c.project_id, table.c.run_id, postgresql_where=text("state = 'pending'"))
    return table


def ops_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """ops_cursors, ops_batches and ops_items, added to ``metadata``."""
    return _cursors(metadata), _batches(metadata), _items(metadata)
