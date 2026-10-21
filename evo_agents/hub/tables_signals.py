"""The tables of signals (0074): what services outside the hub, and members, measure about a project's product, and of
the hub's events (0077), which the Loop reads by cursor. They are the hub's, not a plugin's, since the canvas, the Loop
and the Curator all read them; ``evo_agents.hub.tables`` adds them to its metadata as ``tables.signal_keys``,
``tables.signals``, ``tables.signal_ingest_keys`` and ``tables.hub_events``.

- signal_keys: the registry of a project's keys, each declared by an admin before the hub takes a signal of it: its
  name (such as ``kb.search.zero_result_rate``), its kind (a number, or a piece of feedback in text), its unit,
  description and source, the level a signal of it gets when it names none, and when it was archived.
- signal_ingest_keys: the keys a service writes a project's signals with, ``evi_`` and 32 random bytes, kept as the hex
  SHA-256 alone: a name, the highest level a signal written with it may carry, when it was last used, the window of the
  rate limit, and when it was revoked.
- signals: each signal: its key, its number or its text, its source, when it was measured, the tenant it is about (a
  free dimension of the data; the hub has no tenants), whether it is synthetic, the id its sender gave it, its label,
  who pushed it (an ingest key, a member, or the agent of a run for a member), the batch it came in and when.
- hub_events (0077): each event of a project the hub wrote for the Loop, only ever added to
  (``evo_agents.hub.hub_events``): its kind (``canvas.claim``, ``canvas.baseline``, ``signal.posted``; plan loop-engine
  adds its own), its payload of ids and states, and when; its id only grows, in the order the events of one project
  became visible.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

KEY_NAME = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,5}$"  # signal_keys.name: two to six dotted parts
KINDS = ("number", "feedback")  # signal_keys.kind
VIAS = ("ingest", "member", "run")  # signals.via: an ingest key, a member's credential, the agent of a run
INGEST_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"  # signal_ingest_keys.name
MAX_KEY_NAME_CHARS = 200
MAX_UNIT_CHARS = 50
MAX_DESCRIPTION_CHARS = 2_000
MAX_SOURCE_CHARS = 200
MAX_LEVEL_CHARS = 100  # a level of the project's ladder, as projects.levels names it
MAX_TEXT_BYTES = 4_096  # of a piece of feedback, in UTF-8
MAX_TENANT_CHARS = 100
MAX_EXTERNAL_ID_CHARS = 200


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _keys(metadata: MetaData) -> Table:
    name = "signal_keys"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("unit", Text, nullable=False, server_default=""),
        Column("description", Text, nullable=False, server_default=""),
        Column("source", Text, nullable=False, server_default=""),
        Column("level", Text),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        Column("archived_at", DateTime(timezone=True)),
        Column("archived_by", BigInteger),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "archived_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="signal_keys_pkey"),
        UniqueConstraint("project_id", "name", name="signal_keys_name_key"),
    )


def _ingest_keys(metadata: MetaData) -> Table:
    name = "signal_ingest_keys"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("token_hash", Text, nullable=False),
        Column("max_level", Text, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        Column("last_used_at", DateTime(timezone=True)),
        Column("window_started_at", DateTime(timezone=True)),
        Column("window_batches", Integer, nullable=False, server_default="0"),
        Column("revoked_at", DateTime(timezone=True)),
        Column("revoked_by", BigInteger),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "revoked_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="signal_ingest_keys_pkey"),
        UniqueConstraint("token_hash", name="signal_ingest_keys_token_hash_key"),
    )
    live = table.c.revoked_at.is_(None)
    Index("signal_ingest_keys_name_key", table.c.project_id, table.c.name, unique=True, postgresql_where=live)
    return table


def _signals(metadata: MetaData) -> Table:
    name = "signals"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("key_id", BigInteger, nullable=False),
        Column("value", Double),
        Column("text", Text),
        Column("source", Text, nullable=False, server_default=""),
        Column("measured_at", DateTime(timezone=True), nullable=False),
        Column("tenant", Text),
        Column("synthetic", Boolean, nullable=False, server_default=false()),
        Column("external_id", Text),
        Column("label", JSONB, nullable=False),
        Column("via", Text, nullable=False),
        Column("ingest_key_id", BigInteger),
        Column("user_id", BigInteger),
        Column("run_id", BigInteger),
        Column("batch_id", Uuid, nullable=False),
        _stamp("received_at"),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "key_id", "signal_keys", ondelete="RESTRICT"),
        _key(name, "ingest_key_id", "signal_ingest_keys", ondelete="RESTRICT"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="signals_pkey"),
        Index("signals_key_idx", "key_id", "measured_at"),
        Index("signals_received_idx", "project_id", "received_at"),
    )
    c = table.c
    by_key = (c.ingest_key_id.is_not(None)) & (c.external_id.is_not(None))
    Index("signals_ingest_external_key", c.ingest_key_id, c.external_id, unique=True, postgresql_where=by_key)
    by_member = (c.ingest_key_id.is_(None)) & (c.external_id.is_not(None))
    Index(
        "signals_member_external_key", c.project_id, c.user_id, c.external_id, unique=True, postgresql_where=by_member
    )
    return table


def _events(metadata: MetaData) -> Table:
    name = "hub_events"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("payload", JSONB, nullable=False),
        _stamp("created_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="hub_events_pkey"),
        Index("hub_events_project_idx", "project_id", "id"),
    )


def signal_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    """signal_keys, signals, signal_ingest_keys and hub_events, added to ``metadata`` (which holds projects, users and
    runs already)."""
    keys, ingest_keys = _keys(metadata), _ingest_keys(metadata)
    return keys, _signals(metadata), ingest_keys, _events(metadata)
