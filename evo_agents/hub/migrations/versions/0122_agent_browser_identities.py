"""Browser identities an agent profile owns.

Revision ID: 0122
Revises: 0121
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_browser`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Plan agent-browser, criterion a9. browser_identities.owner_kind takes ``agent``: an identity an agent profile owns, named
by profile_id (new, a foreign key to agent_profiles that restricts: profiles are soft-deleted, and a profile's identities
are deleted by the transaction that deletes it). An identity of an agent has no member owner. profile_default marks the
one a profile hands to every run under it, at most one per profile among the live identities
(browser_identities_profile_default_key). An agent's identity is unique by (profile_id, name) among the live ones
(browser_identities_profile_name_key). browser_identities_check is replaced by the same check with the third owner kind
and the rule that profile_id is set exactly for an agent's identity; every identity there is stays a member's or the
project's, unchanged.

Going back deletes the identities of agents (their blobs are recorded in blob_deletions, for the collection of
unreferenced blobs to delete them from the bucket), drops the columns and the indexes, and puts the check of 0117 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

TABLE = "browser_identities"
CHECK = f"{TABLE}_check"
FKEY = f"{TABLE}_profile_id_fkey"
NAME_KEY = f"{TABLE}_profile_name_key"
DEFAULT_KEY = f"{TABLE}_profile_default_key"
OWNER_KINDS_0117 = ("member", "project")
OWNER_KINDS = (*OWNER_KINDS_0117, "agent")
STATUSES = ("active", "needs_login", "disabled")
NAME = "^[a-z0-9][a-z0-9._-]{0,63}$"
HEX_SHA256 = "^[0-9a-f]{64}$"
MAX_DOMAINS = 50
MAX_CHROMIUM_VERSION_CHARS = 100
KIND = "browser-profile"
VERSION_COLUMNS = ("sha256", "size", "sealed_key", "key_nonce", "key_id")

col = sa.column


def _whole(prefix: str):
    """The five columns of one version, all set or all null, a set hash in hex and a set size positive."""
    first = col(f"{prefix}sha256").is_(None)
    return sa.and_(
        *(first == col(f"{prefix}{name}").is_(None) for name in VERSION_COLUMNS[1:]),
        sa.or_(first, sa.and_(col(f"{prefix}sha256").regexp_match(HEX_SHA256), col(f"{prefix}size") > 0)),
    )


def _check(owner_kinds, *, agents: bool) -> None:
    owned = [(col("owner_kind") == "member") == col("owner_id").is_not(None)]
    if agents:
        owned.append((col("owner_kind") == "agent") == col("profile_id").is_not(None))
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(
        CHECK,
        TABLE,
        sa.and_(
            col("owner_kind").in_(owner_kinds),
            *owned,
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
    )


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("profile_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("profile_default", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_foreign_key(FKEY, TABLE, "agent_profiles", ["profile_id"], ["id"], ondelete="RESTRICT")
    _check(OWNER_KINDS, agents=True)
    live = col("deleted_at").is_(None)
    op.create_index(
        NAME_KEY,
        TABLE,
        ["profile_id", "name"],
        unique=True,
        postgresql_where=sa.and_(col("owner_kind") == "agent", live),
    )
    op.create_index(
        DEFAULT_KEY,
        TABLE,
        ["profile_id"],
        unique=True,
        postgresql_where=sa.and_(col("profile_default"), live),
    )


def downgrade() -> None:
    identities = sa.table(
        TABLE, col("owner_kind"), col("sha256"), col("size"), col("previous_sha256"), col("previous_size")
    )
    deletions = sa.table("blob_deletions", col("sha256"), col("size"), col("kind"))
    for sha256, size in (
        (identities.c.sha256, identities.c.size),
        (identities.c.previous_sha256, identities.c.previous_size),
    ):
        blobs = sa.select(sha256, size, sa.literal(KIND)).where(sha256.is_not(None), identities.c.owner_kind == "agent")
        recorded = pg_insert(deletions).from_select(["sha256", "size", "kind"], blobs)
        op.execute(recorded.on_conflict_do_nothing(index_elements=["sha256"]))
    op.execute(identities.delete().where(identities.c.owner_kind == "agent"))
    op.drop_index(DEFAULT_KEY, table_name=TABLE)
    op.drop_index(NAME_KEY, table_name=TABLE)
    _check(OWNER_KINDS_0117, agents=False)
    op.drop_constraint(FKEY, TABLE, type_="foreignkey")
    op.drop_column(TABLE, "profile_default")
    op.drop_column(TABLE, "profile_id")
