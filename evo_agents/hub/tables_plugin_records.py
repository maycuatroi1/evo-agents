"""The tables of the records an isolated plugin keeps on the hub (0169), which ``evo_agents.hub.tables`` adds to its
metadata as ``tables.plugin_records`` and ``tables.plugin_record_leases``.

plugin_records holds a plugin's records in Postgres, by project, plugin, collection and key, each with its revision,
the level of the project's ladder it carries, whether its writer alone is its audience, who wrote it and when. They sit
in the hub's pg_dump and nothing on the plugin host holds them. plugin_record_leases holds the SHA-256 of each
delegation the hub hands a backend for one call it serves: who the call is for and the cap of what they may read, until
the lease expires (``evo_agents.hub.server.plugin_delegation``).
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


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def plugin_record_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(plugin_records, plugin_record_leases), added to ``metadata``."""
    records = Table(
        "plugin_records",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("collection", Text, nullable=False),
        Column("key", Text, nullable=False),
        Column("revision", Integer, nullable=False, server_default="1"),
        Column("level", Text, nullable=False),
        Column("audience_user_id", BigInteger),
        Column("body", JSONB, nullable=False),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        Column("updated_by", BigInteger),
        _stamp("updated_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="plugin_records_project_id_fkey"
        ),
        ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="RESTRICT", name="plugin_records_plugin_id_fkey"),
        ForeignKeyConstraint(
            ["audience_user_id"], ["users.id"], ondelete="RESTRICT", name="plugin_records_audience_user_id_fkey"
        ),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="plugin_records_created_by_fkey"),
        ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT", name="plugin_records_updated_by_fkey"),
        PrimaryKeyConstraint("project_id", "plugin_id", "collection", "key", name="plugin_records_pkey"),
    )
    leases = Table(
        "plugin_record_leases",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("token_hash", Text, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("run_id", BigInteger),
        Column("run_kind", Text),
        Column("sink", Text),
        Column("max_level", Text),
        _stamp("created_at"),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_record_leases_plugin_id_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="plugin_record_leases_project_id_fkey"
        ),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="plugin_record_leases_user_id_fkey"),
        PrimaryKeyConstraint("id", name="plugin_record_leases_pkey"),
        UniqueConstraint("token_hash", name="plugin_record_leases_token_hash_key"),
        Index("plugin_record_leases_expires_idx", "expires_at"),
    )
    return records, leases
