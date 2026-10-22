"""Browser identities: browser_identities.

Revision ID: 0105
Revises: 0104
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_browser``, ``evo_agents.hub.credentials.SECRET_NAME`` and
``evo_agents.hub.server.browser_profiles`` as of this revision, never imports, so a later change to them takes a new
revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A browser identity is a Chromium profile kept logged in to some sites (plan agent-browser, criterion a2). A member
owns it (owner_kind member, owner_id that member) or the project does (owner_kind project, owner_id null); its name,
of the form of a secret's, is unique among the live identities of its owner in the project (two partial unique
indexes). It lists 1 to MAX_DOMAINS domains, the worker that holds it, its status (active, needs_login, disabled) and
the run holding it. version counts the profiles saved, 0 before the first: the current one is a sealed blob of kind
browser-profile with its data key sealed under EVO_HUB_SECRETS_KEY, and the one before it the previous_* columns.
Each set of five columns is there whole or not at all; a live identity has its current one exactly from version 1 on,
and a deleted one has neither (its keys are gone, its blobs recorded for deletion).

Going back drops the table: the identities are lost, and the blobs of their profiles are recorded in blob_deletions,
for the collection of unreferenced blobs to delete them from the bucket.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

TABLE = "browser_identities"
OWNER_KINDS = ("member", "project")
STATUSES = ("active", "needs_login", "disabled")
NAME = "^[a-z0-9][a-z0-9._-]{0,63}$"
HEX_SHA256 = "^[0-9a-f]{64}$"
MAX_DOMAINS = 50
MAX_CHROMIUM_VERSION_CHARS = 100
KIND = "browser-profile"
VERSION_COLUMNS = ("sha256", "size", "sealed_key", "key_nonce", "key_id")

col = sa.column


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _version(prefix: str) -> list[sa.Column]:
    return [
        sa.Column(f"{prefix}sha256", sa.Text(), nullable=True),
        sa.Column(f"{prefix}size", sa.BigInteger(), nullable=True),
        sa.Column(f"{prefix}sealed_key", sa.LargeBinary(), nullable=True),
        sa.Column(f"{prefix}key_nonce", sa.LargeBinary(), nullable=True),
        sa.Column(f"{prefix}key_id", sa.Text(), nullable=True),
    ]


def _whole(prefix: str):
    """The five columns of one version, all set or all null, a set hash in hex and a set size positive."""
    first = col(f"{prefix}sha256").is_(None)
    return sa.and_(
        *(first == col(f"{prefix}{name}").is_(None) for name in VERSION_COLUMNS[1:]),
        sa.or_(first, sa.and_(col(f"{prefix}sha256").regexp_match(HEX_SHA256), col(f"{prefix}size") > 0)),
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domains", ARRAY(sa.Text()), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("held_by_run", sa.BigInteger(), nullable=True),
        *_version(""),
        sa.Column("chromium_version", sa.Text(), nullable=True),
        _when("last_saved_at"),
        sa.Column("last_saved_by", sa.BigInteger(), nullable=True),
        *_version("previous_"),
        _when("last_used_at"),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("deleted_at"),
        _key("project_id", "projects", ondelete="RESTRICT"),
        _key("owner_id", "users", ondelete="RESTRICT"),
        _key("worker_id", "workers", ondelete="RESTRICT"),
        _key("held_by_run", "runs", ondelete="SET NULL"),
        _key("last_saved_by", "users", ondelete="SET NULL"),
        _key("created_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("owner_kind").in_(OWNER_KINDS),
                (col("owner_kind") == "member") == col("owner_id").is_not(None),
                col("name").regexp_match(NAME),
                sa.func.cardinality(col("domains")).between(1, MAX_DOMAINS),
                col("status").in_(STATUSES),
                col("version") >= 0,
                _whole(""),
                _whole("previous_"),
                sa.or_(col("previous_sha256").is_(None), col("sha256").is_not(None)),
                sa.or_(col("deleted_at").is_not(None), (col("version") == 0) == col("sha256").is_(None)),
                sa.or_(col("deleted_at").is_(None), sa.and_(col("sha256").is_(None), col("previous_sha256").is_(None))),
                sa.or_(
                    col("chromium_version").is_(None),
                    sa.func.char_length(col("chromium_version")).between(1, MAX_CHROMIUM_VERSION_CHARS),
                ),
            ),
            name=f"{TABLE}_check",
        ),
    )
    op.create_index(f"{TABLE}_worker_idx", TABLE, ["worker_id"])
    op.create_index(f"{TABLE}_held_by_run_idx", TABLE, ["held_by_run"])
    live = col("deleted_at").is_(None)
    op.create_index(
        f"{TABLE}_member_name_key",
        TABLE,
        ["project_id", "owner_id", "name"],
        unique=True,
        postgresql_where=sa.and_(col("owner_kind") == "member", live),
    )
    op.create_index(
        f"{TABLE}_project_name_key",
        TABLE,
        ["project_id", "name"],
        unique=True,
        postgresql_where=sa.and_(col("owner_kind") == "project", live),
    )


def downgrade() -> None:
    identities = sa.table(TABLE, col("sha256"), col("size"), col("previous_sha256"), col("previous_size"))
    deletions = sa.table("blob_deletions", col("sha256"), col("size"), col("kind"))
    for sha256, size in (
        (identities.c.sha256, identities.c.size),
        (identities.c.previous_sha256, identities.c.previous_size),
    ):
        blobs = sa.select(sha256, size, sa.literal(KIND)).where(sha256.is_not(None))
        recorded = pg_insert(deletions).from_select(["sha256", "size", "kind"], blobs)
        op.execute(recorded.on_conflict_do_nothing(index_elements=["sha256"]))
    op.drop_table(TABLE)
