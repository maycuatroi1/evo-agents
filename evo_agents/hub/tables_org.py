"""The tables of the org chart (0099), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.org_units``, ``tables.org_seats``, ``tables.autonomy_ceilings``): queries name them there.

org_units holds the tree of units (``evo_agents.hub.org``): its parent (NULL for the root, the one unit of kind org,
which a unique index keeps alone), a slug unique on the hub, a name, the kind (org, team or cohort), the day a cohort
ends, when it was archived (units are never deleted), and its revision, which every change of the unit or of a seat
in it raises by one.

org_seats holds each seat a person or an agent profile holds in a unit, never deleted: exactly one of the user and the
profile; the role (lead, member, intern, or agent for a profile, which always has a sponsor); whether it is the
person's primary seat, at most one live one each (a unique index); the seat it reports to, which an intern's always
names; the sponsor, a user; the days it starts and ends, and when it ended. An agent profile holds at most one live
seat.

autonomy_ceilings holds every revision of the ceiling of a unit or a seat, never changed: the scope (unit or seat), the
id of the unit or the seat, the revision (1 and up within the scope), the level (A0 to A5), the writer's note, who
wrote it (NULL for the root's first, which schema 0099 wrote) and when. A scope's ceiling is its highest revision.

worker_shares (0106, ``tables.worker_shares``) holds the units a shared worker is narrowed to: the worker, the unit, who
added the row and when (``evo_agents.hub.server.worker_shares``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    false,
    func,
)


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _key(table: str, column: str, target: str, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def units_table(metadata: MetaData) -> Table:
    units = Table(
        "org_units",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("parent_id", BigInteger),
        Column("slug", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("ends_on", Date),
        Column("archived_at", DateTime(timezone=True)),
        Column("revision", Integer, nullable=False, server_default="1"),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key("org_units", "parent_id", "org_units", "RESTRICT"),
        _key("org_units", "created_by", "users", "RESTRICT"),
        PrimaryKeyConstraint("id", name="org_units_pkey"),
        UniqueConstraint("slug", name="org_units_slug_key"),
        Index("org_units_parent_idx", "parent_id"),
    )
    Index("org_units_one_root_key", units.c.kind, unique=True, postgresql_where=units.c.parent_id.is_(None))
    return units


def seats_table(metadata: MetaData) -> Table:
    seats = Table(
        "org_seats",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("unit_id", BigInteger, nullable=False),
        Column("user_id", BigInteger),
        Column("profile_id", BigInteger),
        Column("role", Text, nullable=False),
        Column("is_primary", Boolean, nullable=False, server_default=false()),
        Column("reports_to", BigInteger),
        Column("sponsor_id", BigInteger),
        Column("starts_on", Date, nullable=False, server_default=func.current_date()),
        Column("ends_on", Date),
        Column("ended_at", DateTime(timezone=True)),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key("org_seats", "unit_id", "org_units", "RESTRICT"),
        _key("org_seats", "user_id", "users", "CASCADE"),
        _key("org_seats", "profile_id", "agent_profiles", "CASCADE"),
        _key("org_seats", "reports_to", "org_seats", "RESTRICT"),
        _key("org_seats", "sponsor_id", "users", "RESTRICT"),
        _key("org_seats", "created_by", "users", "RESTRICT"),
        PrimaryKeyConstraint("id", name="org_seats_pkey"),
        Index("org_seats_unit_idx", "unit_id"),
        Index("org_seats_user_idx", "user_id"),
    )
    live = seats.c.ended_at.is_(None)
    Index(
        "org_seats_one_primary_key",
        seats.c.user_id,
        unique=True,
        postgresql_where=seats.c.is_primary & live & seats.c.user_id.is_not(None),
    )
    Index(
        "org_seats_one_agent_key",
        seats.c.profile_id,
        unique=True,
        postgresql_where=live & seats.c.profile_id.is_not(None),
    )
    return seats


def ceilings_table(metadata: MetaData) -> Table:
    return Table(
        "autonomy_ceilings",
        metadata,
        Column("scope", Text, nullable=False),
        Column("scope_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("level", Text, nullable=False),
        Column("note", Text),
        Column("author_id", BigInteger),
        _stamp("created_at"),
        _key("autonomy_ceilings", "author_id", "users", "RESTRICT"),
        PrimaryKeyConstraint("scope", "scope_id", "revision", name="autonomy_ceilings_pkey"),
    )


def worker_shares_table(metadata: MetaData) -> Table:
    return Table(
        "worker_shares",
        metadata,
        Column("worker_id", BigInteger, nullable=False),
        Column("unit_id", BigInteger, nullable=False),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        _key("worker_shares", "worker_id", "workers", "CASCADE"),
        _key("worker_shares", "unit_id", "org_units", "RESTRICT"),
        _key("worker_shares", "created_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("worker_id", "unit_id", name="worker_shares_pkey"),
        Index("worker_shares_unit_idx", "unit_id"),
    )


def org_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    """(org_units, org_seats, autonomy_ceilings, worker_shares), added to ``metadata``."""
    return units_table(metadata), seats_table(metadata), ceilings_table(metadata), worker_shares_table(metadata)
