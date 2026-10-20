"""Signals: the registry of a project's keys, the ingest keys services write with, and the signals themselves.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_signals`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Signals are the hub's, not a plugin's, so the tables carry no plugin's prefix. signal_keys holds the keys an admin
declared for a project, each a number or a piece of feedback, unique by name in the project; signal_ingest_keys the
keys a service writes the project's signals with, by the SHA-256 of the token alone, a name unique among the live ones,
the highest level they write and the window of their rate limit; signals each number or piece of feedback (one of the
two, the text at most 4 KiB in UTF-8) with its key, when it was measured, its tenant, whether it is synthetic, the id
its sender gave it (once per ingest key, or per member without one), its label, who pushed it and the batch it came in.

Going back drops the three tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

KEY_NAME = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,5}$"
KINDS = ("number", "feedback")
VIAS = ("ingest", "member", "run")
INGEST_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
TOKEN_HASH = r"^[0-9a-f]{64}$"
MAX_KEY_NAME_CHARS = 200
MAX_UNIT_CHARS = 50
MAX_DESCRIPTION_CHARS = 2_000
MAX_SOURCE_CHARS = 200
MAX_LEVEL_CHARS = 100
MAX_TEXT_BYTES = 4_096
MAX_TENANT_CHARS = 100
MAX_EXTERNAL_ID_CHARS = 200

col = sa.column
TABLES = ("signals", "signal_ingest_keys", "signal_keys")  # in the order going back drops them
INDEXES = (  # (index, table), dropped before the tables
    ("signals_member_external_key", "signals"),
    ("signals_ingest_external_key", "signals"),
    ("signals_received_idx", "signals"),
    ("signals_key_idx", "signals"),
    ("signal_ingest_keys_name_key", "signal_ingest_keys"),
)


def _key(table: str, column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _stamp(name: str):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _keys() -> None:
    name = "signal_keys"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.Text(), server_default="", nullable=False),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.BigInteger(), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "archived_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="signal_keys_pkey"),
        sa.UniqueConstraint("project_id", "name", name="signal_keys_name_key"),
        sa.CheckConstraint(
            sa.and_(
                _length("name", 1, MAX_KEY_NAME_CHARS),
                col("name").regexp_match(KEY_NAME),
                col("kind").in_(KINDS),
                sa.func.char_length(col("unit")) <= MAX_UNIT_CHARS,
                sa.func.char_length(col("description")) <= MAX_DESCRIPTION_CHARS,
                sa.func.char_length(col("source")) <= MAX_SOURCE_CHARS,
                _length("level", 1, MAX_LEVEL_CHARS),
                col("archived_at").is_(None) == col("archived_by").is_(None),
            ),
            name="signal_keys_check",
        ),
    )


def _ingest_keys() -> None:
    name = "signal_ingest_keys"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("max_level", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_batches", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "revoked_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="signal_ingest_keys_pkey"),
        sa.UniqueConstraint("token_hash", name="signal_ingest_keys_token_hash_key"),
        sa.CheckConstraint(
            sa.and_(
                col("name").regexp_match(INGEST_NAME),
                col("token_hash").regexp_match(TOKEN_HASH),
                _length("max_level", 1, MAX_LEVEL_CHARS),
                col("window_batches") >= 0,
                col("revoked_at").is_(None) == col("revoked_by").is_(None),
            ),
            name="signal_ingest_keys_check",
        ),
    )
    op.create_index(
        "signal_ingest_keys_name_key",
        name,
        ["project_id", "name"],
        unique=True,
        postgresql_where=col("revoked_at").is_(None),
    )


def _who_pushed():
    """An ingest key, or a member (the agent of a run pushing for one is a member too, with its run)."""
    by_key = sa.and_(col("via") == "ingest", col("ingest_key_id").is_not(None), col("user_id").is_(None))
    by_member = sa.and_(col("via").in_(("member", "run")), col("ingest_key_id").is_(None), col("user_id").is_not(None))
    return sa.and_(
        col("via").in_(VIAS), sa.or_(by_key, by_member), sa.or_(col("run_id").is_(None), col("via") == "run")
    )


def _signals() -> None:
    name = "signals"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("key_id", sa.BigInteger(), nullable=False),
        sa.Column("value", sa.Double(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default="", nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant", sa.Text(), nullable=True),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("ingest_key_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        _stamp("received_at"),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "key_id", "signal_keys", ondelete="RESTRICT"),
        _key(name, "ingest_key_id", "signal_ingest_keys", ondelete="RESTRICT"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="signals_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("value").is_(None) != col("text").is_(None),
                sa.func.octet_length(col("text")).between(1, MAX_TEXT_BYTES),
                sa.func.char_length(col("source")) <= MAX_SOURCE_CHARS,
                _length("tenant", 1, MAX_TENANT_CHARS),
                _length("external_id", 1, MAX_EXTERNAL_ID_CHARS),
                sa.func.jsonb_typeof(col("label")) == "object",
                _who_pushed(),
            ),
            name="signals_check",
        ),
    )
    op.create_index("signals_key_idx", name, ["key_id", "measured_at"])
    op.create_index("signals_received_idx", name, ["project_id", "received_at"])
    op.create_index(
        "signals_ingest_external_key",
        name,
        ["ingest_key_id", "external_id"],
        unique=True,
        postgresql_where=sa.and_(col("ingest_key_id").is_not(None), col("external_id").is_not(None)),
    )
    op.create_index(
        "signals_member_external_key",
        name,
        ["project_id", "user_id", "external_id"],
        unique=True,
        postgresql_where=sa.and_(col("ingest_key_id").is_(None), col("external_id").is_not(None)),
    )


def upgrade() -> None:
    _keys()
    _ingest_keys()
    _signals()


def downgrade() -> None:
    for index, table in INDEXES:
        op.drop_index(index, table_name=table)
    for name in TABLES:
        op.drop_table(name)
