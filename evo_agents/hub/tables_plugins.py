"""The tables of the hub's store of plugins (0033), which ``evo_agents.hub.tables`` adds to its metadata and exposes as
its own attributes (``tables.plugins``, ``tables.plugin_versions``): queries name them there.

plugins holds each plugin the hub knows, by the id its manifest gives (``name``): the publisher of its first version,
which every later version must have too, which version is installed on the hub (NULL when none is), who installed it
and when, and, when the installed version stopped running on this release of evo-agents, why and since when the hub
turned it off (``disabled_reason``, ``disabled_at``). plugin_versions holds every version published, never changed:
the bundle's SHA-256, size and key in the blob store, the manifest as the bundle holds it, its publisher and tier, the
signature envelope beside the bundle (``evo_agents.plugins.signing``), its kind and who signed (the workflow identity
of a Sigstore certificate, or the id of an Ed25519 key), who published it and when.

The installed version is a version of the plugin: (id, installed_version) refers to plugin_versions, a key created
after both tables (``use_alter``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
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
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def plugin_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(plugins, plugin_versions), added to ``metadata``."""
    plugins = Table(
        "plugins",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("name", Text, nullable=False),
        Column("publisher", Text, nullable=False),
        Column("installed_version", Text),
        Column("installed_by", BigInteger),
        Column("installed_at", DateTime(timezone=True)),
        Column("disabled_reason", Text),
        Column("disabled_at", DateTime(timezone=True)),
        _stamp("created_at"),
        ForeignKeyConstraint(["installed_by"], ["users.id"], ondelete="RESTRICT", name="plugins_installed_by_fkey"),
        ForeignKeyConstraint(
            ["id", "installed_version"],
            ["plugin_versions.plugin_id", "plugin_versions.version"],
            ondelete="RESTRICT",
            name="plugins_installed_version_fkey",
            use_alter=True,
        ),
        PrimaryKeyConstraint("id", name="plugins_pkey"),
        UniqueConstraint("name", name="plugins_name_key"),
    )
    plugin_versions = Table(
        "plugin_versions",
        metadata,
        Column("plugin_id", BigInteger, nullable=False),
        Column("version", Text, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("r2_key", Text, nullable=False),
        Column("publisher", Text, nullable=False),
        Column("tier", Text, nullable=False),
        Column("manifest", JSONB, nullable=False),
        Column("signature", JSONB, nullable=False),
        Column("signature_kind", Text, nullable=False),
        Column("signed_by", Text, nullable=False),
        Column("published_by", BigInteger, nullable=False),
        _stamp("published_at"),
        ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_versions_plugin_id_fkey"),
        ForeignKeyConstraint(
            ["published_by"], ["users.id"], ondelete="RESTRICT", name="plugin_versions_published_by_fkey"
        ),
        PrimaryKeyConstraint("plugin_id", "version", name="plugin_versions_pkey"),
    )
    return plugins, plugin_versions
