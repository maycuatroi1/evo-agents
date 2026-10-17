"""Plugin projects: which projects enabled which installed plugin with which permissions granted, the versions of each
layer of a plugin's config, a run's own layer, and the plugins a run ran with.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-16

``evo_agents.hub.tables_plugins`` describes the tables; the lists below are copies of ``evo_agents.plugins`` as of
this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation
on SQLAlchemy Core expressions.

project_plugins holds one row per plugin a project enabled: the permissions of the plugin's manifest that an admin of
the project granted (some of PERMISSIONS, each once), who enabled it and when, and who granted the permissions last
and when. Turning a plugin off for the project deletes its row, and its grants with it.

plugin_configs holds every version of each stored layer of a plugin's config, never changed: the layer (instance,
the hub's own; project; agent, an agent profile of the project), the version (1 and up within the layer), the plain
values it sets (an object), its secrets sealed under EVO_HUB_SECRETS_KEY (an object of field name to ciphertext,
nonce and key_id, in base64), the version a rollback copied (below its own), who wrote it and when. A layer's
current version is its highest.

runs.plugin_config is a run's own layer, given when it was dispatched: an object of plugin name to the plain values
it sets, NULL for none. run_plugins holds, per run and plugin enabled on its project when a worker claimed it, the
version installed then and the digest of the effective config (sha256:<hex>), written again at a later claim.

Going back drops the three tables and the column, which is the schema that schema 0033 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

PERMISSIONS = (  # evo_agents.plugins.manifest.PERMISSIONS
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
)
DIGEST = "^sha256:[0-9a-f]{64}$"

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _object(name: str) -> sa.ColumnElement:
    return sa.func.jsonb_typeof(col(name)) == "object"


def _create_project_plugins() -> None:
    op.create_table(
        "project_plugins",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("enabled_by", sa.BigInteger(), nullable=False),
        _stamp("enabled_at"),
        sa.Column("granted_by", sa.BigInteger(), nullable=False),
        _stamp("granted_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="project_plugins_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="project_plugins_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["enabled_by"], ["users.id"], ondelete="RESTRICT", name="project_plugins_enabled_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"], ["users.id"], ondelete="RESTRICT", name="project_plugins_granted_by_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "plugin_id", name="project_plugins_pkey"),
        sa.CheckConstraint(
            col("permissions").op("<@")(postgresql.array(PERMISSIONS, type_=sa.Text())),
            name="project_plugins_permissions_check",
        ),
    )
    op.create_index("project_plugins_plugin_idx", "project_plugins", ["plugin_id", "project_id"])


def _create_configs() -> None:
    op.create_table(
        "plugin_configs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("secrets", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("rolled_back_to", sa.Integer(), nullable=True),
        sa.Column("changed_by", sa.BigInteger(), nullable=False),
        _stamp("changed_at"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_configs_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="plugin_configs_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="plugin_configs_profile_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["users.id"], ondelete="RESTRICT", name="plugin_configs_changed_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="plugin_configs_pkey"),
        sa.UniqueConstraint(
            "plugin_id",
            "layer",
            "project_id",
            "profile_id",
            "version",
            name="plugin_configs_version_key",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            sa.or_(
                sa.and_(col("layer") == "instance", col("project_id").is_(None), col("profile_id").is_(None)),
                sa.and_(col("layer") == "project", col("project_id").is_not(None), col("profile_id").is_(None)),
                sa.and_(col("layer") == "agent", col("project_id").is_not(None), col("profile_id").is_not(None)),
            ),
            name="plugin_configs_layer_check",
        ),
        sa.CheckConstraint(
            sa.and_(
                col("version") >= 1,
                sa.or_(col("rolled_back_to").is_(None), col("rolled_back_to").between(1, col("version") - 1)),
                _object("values"),
                _object("secrets"),
            ),
            name="plugin_configs_check",
        ),
    )


def _create_run_plugins() -> None:
    op.add_column("runs", sa.Column("plugin_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint(
        "runs_plugin_config_check", "runs", sa.or_(col("plugin_config").is_(None), _object("plugin_config"))
    )
    op.create_table(
        "run_plugins",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("config_digest", sa.Text(), nullable=False),
        _stamp("recorded_at"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_plugins_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["plugin_id", "version"],
            ["plugin_versions.plugin_id", "plugin_versions.version"],
            ondelete="RESTRICT",
            name="run_plugins_version_fkey",
        ),
        sa.PrimaryKeyConstraint("run_id", "plugin_id", name="run_plugins_pkey"),
        sa.CheckConstraint(col("config_digest").regexp_match(DIGEST), name="run_plugins_digest_check"),
    )


def upgrade() -> None:
    _create_project_plugins()
    _create_configs()
    _create_run_plugins()


def downgrade() -> None:
    op.drop_table("run_plugins")
    op.drop_constraint("runs_plugin_config_check", "runs", type_="check")
    op.drop_column("runs", "plugin_config")
    op.drop_table("plugin_configs")
    op.drop_index("project_plugins_plugin_idx", table_name="project_plugins")
    op.drop_table("project_plugins")
