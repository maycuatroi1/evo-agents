"""The records an isolated plugin keeps on the hub: plugin_records, the leases of its calls and the permission records:use.

Revision ID: 0169
Revises: 0168
Create Date: 2026-10-27

The names below are copies of ``evo_agents.hub.tables_plugin_records`` and ``evo_agents.plugins.manifest`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

plugin_records holds the records of an isolated plugin in Postgres, one row for each (project, plugin, collection,
key): a JSON object, its revision, the level of the project's ladder it carries, the writer as its audience when
the plugin asked for that, who wrote and last wrote it and when. plugin_record_leases holds the SHA-256 of the
delegation the hub gives a backend for one call it serves, with the person the call is for, the run and sink the call
reads through and the highest level that person's grant reached, until it expires. project_plugins gains
``records_level``, the highest level a call with no caller (a job, an event) touches records at in that project, which
the admin who enables the plugin sets; and its permissions take ``records:use``.

Going back drops both tables, which loses the records, takes the permission out of every grant and drops the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0169"
down_revision = "0168"
branch_labels = None
depends_on = None

RECORDS = "plugin_records"
LEASES = "plugin_record_leases"
COLLECTION = r"^[a-z][a-z0-9_-]{0,62}$"
MAX_KEY_CHARS = 200
NEW_PERMISSION = "records:use"
PERMISSIONS_0092 = (  # project_plugins.permissions as 0092 set it
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
    "canvas:read",
    "signals:read",
    "srs:read",
    "tasks:read",
)
PERMISSIONS = (*PERMISSIONS_0092, NEW_PERMISSION)  # evo_agents.plugins.manifest.PERMISSIONS

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _permissions(names) -> None:
    op.drop_constraint("project_plugins_permissions_check", "project_plugins", type_="check")
    granted = col("permissions").op("<@")(postgresql.array(names, type_=sa.Text()))
    op.create_check_constraint("project_plugins_permissions_check", "project_plugins", granted)


def _create_records() -> None:
    op.create_table(
        RECORDS,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("audience_user_id", sa.BigInteger(), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="plugin_records_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="RESTRICT", name="plugin_records_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["audience_user_id"], ["users.id"], ondelete="RESTRICT", name="plugin_records_audience_user_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT", name="plugin_records_created_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="RESTRICT", name="plugin_records_updated_by_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "plugin_id", "collection", "key", name="plugin_records_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("collection").regexp_match(COLLECTION),
                sa.func.char_length(col("key")).between(1, MAX_KEY_CHARS),
                col("revision") >= 1,
                sa.func.jsonb_typeof(col("body")) == "object",
            ),
            name="plugin_records_check",
        ),
    )


def _create_leases() -> None:
    op.create_table(
        LEASES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("run_kind", sa.Text(), nullable=True),
        sa.Column("sink", sa.Text(), nullable=True),
        sa.Column("max_level", sa.Text(), nullable=True),
        _stamp("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_record_leases_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="plugin_record_leases_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="plugin_record_leases_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="plugin_record_leases_pkey"),
        sa.UniqueConstraint("token_hash", name="plugin_record_leases_token_hash_key"),
    )
    op.create_index("plugin_record_leases_expires_idx", LEASES, ["expires_at"])


def upgrade() -> None:
    op.add_column("project_plugins", sa.Column("records_level", sa.Text(), nullable=True))
    _permissions(PERMISSIONS)
    _create_records()
    _create_leases()


def downgrade() -> None:
    op.drop_table(LEASES)
    op.drop_table(RECORDS)
    granted = sa.table("project_plugins", col("permissions"))
    op.execute(granted.update().values(permissions=sa.func.array_remove(granted.c.permissions, NEW_PERMISSION)))
    _permissions(PERMISSIONS_0092)
    op.drop_column("project_plugins", "records_level")
