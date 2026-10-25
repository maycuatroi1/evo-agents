"""The tables of the plugin market (0149), which ``evo_agents.hub.tables`` adds to its metadata as
``tables.market_sources``, ``tables.plugin_publishers`` and ``tables.market_install_requests``.

A market source (``market_sources``) is a GitHub repo holding a market's index (``evo_agents.plugins.market``) at
``path`` on ``ref``. ``token`` is a GitHub token of a private repo, sealed with the hub's key (``{ciphertext, nonce,
key_id}``, as a secret field of a plugin's config is); ``builtin`` marks the market the operator names in
EVO_HUB_MARKET_DEFAULT_SOURCE, which the hub adds on its first refresh. A refresh keeps the last index that read
(``index``), the commit and ETag it came from, and ``releases``: plugin id -> the latest release of each free isolated
plugin, ``{tag, version, permissions}`` read from its bundle's manifest, or ``{error}``. A refresh that fails writes
``last_error`` and keeps the rest.

A trusted publisher (``plugin_publishers``) adds a signer to a publisher besides EVO_HUB_PLUGIN_PUBLISHERS: a hub admin
trusted it when installing from a market or a GitHub release. Removing it keeps the row with ``removed_at``.

An install request (``market_install_requests``) is a project admin's ask that a hub admin install a plugin of a
market; one request per project and plugin is open at a time. The CHECK on ``state`` is the migration's alone.
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
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

REQUEST_STATES = ("open", "installed", "declined")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _user(table: str, column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], ["users.id"], ondelete="RESTRICT", name=f"{table}_{column}_fkey")


def market_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """market_sources, plugin_publishers and market_install_requests (0149), added to ``metadata``."""
    sources = Table(
        "market_sources",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("repo", Text, nullable=False),  # OWNER/REPO
        Column("ref", Text, nullable=False, server_default="main"),
        Column("path", Text, nullable=False, server_default="market.json"),
        Column("token", JSONB),  # sealed
        Column("builtin", Boolean, nullable=False, server_default=false()),
        Column("added_by", BigInteger),  # NULL for the builtin source
        _stamp("added_at"),
        _when("fetched_at"),
        Column("fetched_sha", Text),
        Column("etag", Text),
        Column("last_error", Text),
        Column("index", JSONB),
        Column("releases", JSONB, nullable=False, server_default="{}"),
        _user("market_sources", "added_by"),
        PrimaryKeyConstraint("id", name="market_sources_pkey"),
        UniqueConstraint("repo", "ref", "path", name="market_sources_repo_ref_path_key"),
    )
    Index("market_sources_builtin_key", sources.c.builtin, unique=True, postgresql_where=sources.c.builtin)
    publishers = Table(
        "plugin_publishers",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("publisher", Text, nullable=False),
        Column("signer", Text, nullable=False),  # sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY
        Column("added_by", BigInteger, nullable=False),
        _stamp("added_at"),
        Column("removed_by", BigInteger),
        _when("removed_at"),
        Column("note", Text),
        _user("plugin_publishers", "added_by"),
        _user("plugin_publishers", "removed_by"),
        PrimaryKeyConstraint("id", name="plugin_publishers_pkey"),
    )
    p = publishers.c
    Index("plugin_publishers_signer_key", p.publisher, p.signer, unique=True, postgresql_where=p.removed_at.is_(None))
    requests = Table(
        "market_install_requests",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("source_id", BigInteger, nullable=False),
        Column("plugin", Text, nullable=False),
        Column("requested_by", BigInteger, nullable=False),
        _stamp("requested_at"),
        Column("state", Text, nullable=False, server_default="open"),
        Column("decided_by", BigInteger),
        _when("decided_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="market_install_requests_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["source_id"], ["market_sources.id"], ondelete="CASCADE", name="market_install_requests_source_id_fkey"
        ),
        _user("market_install_requests", "requested_by"),
        _user("market_install_requests", "decided_by"),
        PrimaryKeyConstraint("id", name="market_install_requests_pkey"),
    )
    r = requests.c
    Index(
        "market_install_requests_open_key",
        r.project_id,
        r.source_id,
        r.plugin,
        unique=True,
        postgresql_where=r.state == "open",
    )
    return sources, publishers, requests
