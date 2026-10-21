"""The tables of the Loop of a project (0093, plan loop-engine), which ``evo_agents.hub.tables`` adds to its metadata and
exposes as its own attributes (``tables.loop_states``, ``tables.loop_items``, ``tables.loop_item_events``): queries
name them there. They are the stable part of contract loop/1 (``evo_agents.hub.server.loop``): a later engine reads
and writes the same rows.

- loop_states: one row per project whose Loop was ever turned on: whether it is on now (the hub writes it with the
  revision of the policy that sets ``loop.enabled``), when it was turned on last, the cursor (the id of the last row of
  hub_events the Loop read), its breaker (armed or tripped, why, when, who reset it last and when) and when its last
  tick ended.
- loop_items: each work item of a project, only the Loop writes them: its kind and cycle, its key (unique in the
  project, so the same delta never makes a second one), its ancestors (the goal, the claim, the SRS document, use case
  and version, the frame task, the plan, the run in flight, the work item it came of), where it stands and why, what it
  waits on (``detail``), how often a run was handed out for it and how often one failed, who it stands for, the engine
  that made it, the event that made it, and when it was made, changed and finished.
- loop_item_events: what happened to each work item, only ever added to: made, ready, admitted, handed a run,
  waiting, failed, triaged, asked, done, cancelled, or a result noted in its chain; the state and reason after it, the
  run and the event of hub_events it came of.
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
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

FINISHED = ("done", "cancelled")  # loop.model.FINISHED: the states a work item never leaves


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _states(metadata: MetaData) -> Table:
    name = "loop_states"
    return Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("enabled", Boolean, nullable=False, server_default=false()),
        _when("enabled_at"),
        Column("cursor", BigInteger, nullable=False, server_default="0"),
        Column("breaker", Text, nullable=False, server_default="armed"),
        Column("breaker_reason", Text),
        _when("tripped_at"),
        Column("resumed_by", BigInteger),
        _when("resumed_at"),
        _when("ticked_at"),
        _stamp("updated_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "resumed_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("project_id", name="loop_states_pkey"),
    )


def _items(metadata: MetaData) -> Table:
    name = "loop_items"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("cycle", Text, nullable=False),
        Column("key", Text, nullable=False),
        Column("goal_id", BigInteger),
        Column("claim_id", BigInteger),
        Column("srs_document_id", BigInteger),
        Column("use_case", Text),
        Column("srs_version", Integer),
        Column("task_id", BigInteger),
        Column("plan_id", Text),
        Column("run_id", BigInteger),
        Column("parent_id", BigInteger),
        Column("state", Text, nullable=False, server_default="queued"),
        Column("reason", Text),
        Column("detail", JSONB, nullable=False, server_default="{}"),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("failures", Integer, nullable=False, server_default="0"),
        Column("owner", Text),
        Column("engine", Text, nullable=False),
        Column("event_id", BigInteger),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("finished_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "goal_id", "goals", ondelete="SET NULL"),
        _key(name, "claim_id", "canvas_claims", ondelete="SET NULL"),
        _key(name, "srs_document_id", "srs_documents", ondelete="SET NULL"),
        _key(name, "task_id", "tasks_items", ondelete="SET NULL"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "parent_id", "loop_items", ondelete="SET NULL"),
        _key(name, "event_id", "hub_events", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="loop_items_pkey"),
        UniqueConstraint("project_id", "key", name="loop_items_key_key"),
    )
    Index(
        "loop_items_open_idx",
        table.c.project_id,
        table.c.state,
        postgresql_where=table.c.state.not_in(FINISHED),
    )
    Index("loop_items_claim_idx", table.c.claim_id, postgresql_where=table.c.claim_id.is_not(None))
    Index("loop_items_run_idx", table.c.run_id, postgresql_where=table.c.run_id.is_not(None))
    return table


def _events(metadata: MetaData) -> Table:
    name = "loop_item_events"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("item_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("reason", Text),
        Column("run_id", BigInteger),
        Column("event_id", BigInteger),
        Column("detail", JSONB, nullable=False, server_default="{}"),
        _stamp("at"),
        _key(name, "item_id", "loop_items", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "event_id", "hub_events", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="loop_item_events_pkey"),
        Index("loop_item_events_item_idx", "item_id", "id"),
    )
    Index("loop_item_events_run_idx", table.c.run_id, postgresql_where=table.c.run_id.is_not(None))
    return table


def loop_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(loop_states, loop_items, loop_item_events), added to ``metadata``."""
    return _states(metadata), _items(metadata), _events(metadata)
