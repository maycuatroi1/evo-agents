"""The plugin market: the GitHub repos a hub reads markets from, the publishers a hub admin trusted, install requests.

Revision ID: 0154
Revises: 0153
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_market`` and ``evo_agents.plugins.market`` as of this revision,
never imports. Every statement is an Alembic operation on SQLAlchemy Core expressions.

market_sources holds one row per market a hub reads (plan plugin-market, criterion a5): its repo OWNER/REPO, the ref
and path of its index, a GitHub token sealed with the hub's key or NULL, whether it is the operator's default market
(at most one row is), who added it and when, and what the last refresh kept: when it ran, the commit and ETag of the
index, the error of the last refresh that failed, the index that read and the latest release of each of its plugins.
The migration adds no row: the first refresh adds the default market.

plugin_publishers holds the signers a hub admin trusted for a publisher (criterion a6), besides those of
EVO_HUB_PLUGIN_PUBLISHERS: who added it and when, a note, and who removed it and when; one row per publisher and signer
is in force at a time. market_install_requests holds a project admin's request that a hub admin install a plugin of a
market, in state open, installed or declined, who decided it and when exactly when it is not open; one request per
project, market and plugin is open at a time. The rows of a project and a market go with them; a member a row names is
not deleted under it.

Going back drops the three tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None

REPO = "^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"  # evo_agents.plugins.market.REPO
PUBLISHER = "^[a-z0-9][a-z0-9-]{0,62}$"  # evo_agents.plugins.signing.PUBLISHER_ID
SIGNER = "^(sigstore|ed25519):[^[:space:]]+$"  # what follows ID= in EVO_HUB_PLUGIN_PUBLISHERS
PLUGIN = "^[a-z][a-z0-9-]{0,62}$"  # a manifest's id
STATES = ("open", "installed", "declined")  # evo_agents.hub.tables_market.REQUEST_STATES
MAX_REF_CHARS = 255
MAX_PATH_CHARS = 500
MAX_ERROR_CHARS = 2000
MAX_NOTE_CHARS = 500

col = sa.column


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _sources() -> None:
    length = sa.func.char_length
    op.create_table(
        "market_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), server_default="main", nullable=False),
        sa.Column("path", sa.Text(), server_default="market.json", nullable=False),
        sa.Column("token", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("builtin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        _stamp("added_at"),
        _when("fetched_at"),
        sa.Column("fetched_sha", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("index", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("releases", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        _key("market_sources", "added_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_sources_pkey"),
        sa.UniqueConstraint("repo", "ref", "path", name="market_sources_repo_ref_path_key"),
        sa.CheckConstraint(
            sa.and_(
                col("repo").regexp_match(REPO),
                length(col("ref")).between(1, MAX_REF_CHARS),
                length(col("path")).between(1, MAX_PATH_CHARS),
                length(col("last_error")) <= MAX_ERROR_CHARS,
                sa.or_(col("builtin"), col("added_by").is_not(None)),
            ),
            name="market_sources_check",
        ),
    )
    op.create_index(
        "market_sources_builtin_key", "market_sources", ["builtin"], unique=True, postgresql_where=col("builtin")
    )


def _publishers() -> None:
    op.create_table(
        "plugin_publishers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        _stamp("added_at"),
        sa.Column("removed_by", sa.BigInteger(), nullable=True),
        _when("removed_at"),
        sa.Column("note", sa.Text(), nullable=True),
        _key("plugin_publishers", "added_by", "users", "RESTRICT"),
        _key("plugin_publishers", "removed_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="plugin_publishers_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("publisher").regexp_match(PUBLISHER),
                col("signer").regexp_match(SIGNER),
                sa.func.char_length(col("note")) <= MAX_NOTE_CHARS,
                col("removed_at").is_(None) == col("removed_by").is_(None),
            ),
            name="plugin_publishers_check",
        ),
    )
    op.create_index(
        "plugin_publishers_signer_key",
        "plugin_publishers",
        ["publisher", "signer"],
        unique=True,
        postgresql_where=col("removed_at").is_(None),
    )


def _requests() -> None:
    state = col("state")
    op.create_table(
        "market_install_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        _stamp("requested_at"),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        _when("decided_at"),
        _key("market_install_requests", "project_id", "projects", "CASCADE"),
        _key("market_install_requests", "source_id", "market_sources", "CASCADE"),
        _key("market_install_requests", "requested_by", "users", "RESTRICT"),
        _key("market_install_requests", "decided_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="market_install_requests_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("plugin").regexp_match(PLUGIN),
                state.in_(STATES),
                (state == "open") == col("decided_at").is_(None),
                (state == "open") == col("decided_by").is_(None),
            ),
            name="market_install_requests_check",
        ),
    )
    op.create_index(
        "market_install_requests_open_key",
        "market_install_requests",
        ["project_id", "source_id", "plugin"],
        unique=True,
        postgresql_where=state == "open",
    )


def upgrade() -> None:
    _sources()
    _publishers()
    _requests()


def downgrade() -> None:
    op.drop_table("market_install_requests")
    op.drop_table("plugin_publishers")
    op.drop_table("market_sources")
