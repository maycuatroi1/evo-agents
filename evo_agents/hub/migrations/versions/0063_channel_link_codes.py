"""The one-time codes a member links a channel of any kind with, not Telegram's alone.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-20

telegram_links (schemas 0015 and 0016) becomes channel_link_codes, the hub's table of the codes the API of core plugins
makes and redeems (``evo_agents.hub.server.core_plugin_api``, ``link_code`` and ``redeem_link_code``), and gains the
column kind: the kind of members' channel a code links, of the pattern a notification channel's kind has. Every code
there is was Telegram's, so its row becomes kind 'telegram'; a code of one kind never links a channel of another. The
table keeps its rows, its identity and its checks; its constraints and its index take the new table's name. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the table.

Going back deletes the codes of every kind but telegram, which schema 0062 cannot hold, drops the column and gives the
table, its constraints and its index their old names, which is the schema that schema 0062 runs on. A code that was
waiting is a code of ten minutes at most: its member makes a new one.
"""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

OLD, NEW = "telegram_links", "channel_link_codes"
CODE_HASH_CHARS = 64  # a SHA-256 in hex
LINK_SECONDS = 600  # evo_agents.hub.telegram.LINK_SECONDS
KIND = "^[a-z][a-z0-9_-]{0,31}$"  # notification_channels.kind, as schema 0010 checks it (and never web)

col = sa.column


def _checks(table: str) -> list[tuple[str, sa.ColumnElement]]:
    return [
        (
            f"{table}_code_hash_check",
            sa.and_(sa.func.char_length(col("code_hash")) == CODE_HASH_CHARS, col("code_hash").op("~")("^[0-9a-f]+$")),
        ),
        (
            f"{table}_expires_at_check",
            sa.extract("epoch", col("expires_at") - col("created_at")).between(1, LINK_SECONDS),
        ),
        (f"{table}_used_at_check", col("used_at") >= col("created_at")),
    ]


def _drop_keys(table: str) -> None:
    """Drop the checks, keys and index of ``table``, named after it."""
    for name, _ in _checks(table):
        op.drop_constraint(name, table, type_="check")
    op.drop_constraint(f"{table}_token_id_fkey", table, type_="foreignkey")
    op.drop_constraint(f"{table}_user_id_fkey", table, type_="foreignkey")
    op.drop_constraint(f"{table}_code_hash_key", table, type_="unique")
    op.drop_constraint(f"{table}_pkey", table, type_="primary")
    op.drop_index(f"{table}_user_idx", table_name=table)


def _make_keys(table: str) -> None:
    """Make the checks, keys and index of 0015 and 0016 on ``table``, named after it."""
    for name, condition in _checks(table):
        op.create_check_constraint(name, table, condition)
    op.create_primary_key(f"{table}_pkey", table, ["id"])
    op.create_unique_constraint(f"{table}_code_hash_key", table, ["code_hash"])
    op.create_foreign_key(f"{table}_user_id_fkey", table, "users", ["user_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key(f"{table}_token_id_fkey", table, "tokens", ["token_id"], ["id"], ondelete="CASCADE")
    op.create_index(f"{table}_user_idx", table, ["user_id"])


def upgrade() -> None:
    _drop_keys(OLD)
    op.rename_table(OLD, NEW)
    op.add_column(NEW, sa.Column("kind", sa.Text(), nullable=False, server_default="telegram"))
    op.alter_column(NEW, "kind", existing_type=sa.Text(), existing_nullable=False, server_default=None)
    op.create_check_constraint(f"{NEW}_kind_check", NEW, sa.and_(col("kind").op("~")(KIND), col("kind") != "web"))
    _make_keys(NEW)


def downgrade() -> None:
    codes = sa.table(NEW, col("kind"))
    op.execute(codes.delete().where(codes.c.kind != "telegram"))
    _drop_keys(NEW)
    op.drop_constraint(f"{NEW}_kind_check", NEW, type_="check")
    op.drop_column(NEW, "kind")
    op.rename_table(NEW, OLD)
    _make_keys(OLD)
