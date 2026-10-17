"""Plugin backends: the tokens the hub gives the backends of isolated plugins, and what the plugin host says of each.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-17

``evo_agents.hub.tables_plugins`` describes the tables; the lists below are copies of it as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

plugin_tokens holds the SHA-256 (hex) of each token the hub gave the backend of a plugin when the plugin host started
it, when, and when it was revoked; starting the backend again revokes the plugin's older ones, and removing the plugin
deletes them with it.

plugin_backends holds one row per backend the plugin host runs, as the host last said: the version, the state
(placing, starting, running, backoff), the isolation (uid: a user of its own; process: the host's user), its process
id, how many times it was started again, since when it runs, its last failure, when it is tried again, and when the
host said so.

Going back drops the two tables, which is the schema that schema 0034 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

STATES = ("placing", "starting", "running", "backoff")  # tables_plugins.BACKEND_STATES
ISOLATIONS = ("uid", "process")  # tables_plugins.ISOLATIONS
HEX_SHA256 = "^[0-9a-f]{64}$"

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "plugin_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _stamp("created_at"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_tokens_plugin_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="plugin_tokens_pkey"),
        sa.UniqueConstraint("token_hash", name="plugin_tokens_token_hash_key"),
        sa.CheckConstraint(col("token_hash").op("~")(HEX_SHA256), name="plugin_tokens_token_hash_check"),
    )
    op.create_index("plugin_tokens_plugin_idx", "plugin_tokens", ["plugin_id"])
    op.create_table(
        "plugin_backends",
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("isolation", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("restarts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        _stamp("reported_at"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_backends_plugin_id_fkey"
        ),
        sa.PrimaryKeyConstraint("plugin_id", name="plugin_backends_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("state").in_(STATES),
                col("isolation").in_(ISOLATIONS),
                col("restarts") >= 0,
                sa.func.length(sa.func.coalesce(col("error"), "")) <= 4000,
            ),
            name="plugin_backends_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("plugin_backends")
    op.drop_index("plugin_tokens_plugin_idx", table_name="plugin_tokens")
    op.drop_table("plugin_tokens")
