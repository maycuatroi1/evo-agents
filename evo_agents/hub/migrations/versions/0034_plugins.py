"""Plugins: the hub's store of signed plugin bundles, which version of each is installed, and the notice that the hub
turned one off.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-16

``evo_agents.hub.tables_plugins`` describes the two tables; the lists below are copies of ``evo_agents.hub.runs`` and
``evo_agents.plugins`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

plugins holds one row per plugin, by the id its manifest gives (``name``, as a manifest's pattern allows it), with the
publisher every version must have, the version installed (NULL when none is), who installed it and when, and why and
since when the hub turned the installed version off; a reason goes with its time, and an installed version with who
installed it and when. plugin_versions holds every version published, never changed: the bundle's SHA-256, size and
blob key, the manifest, publisher, tier (isolated or core), the signature envelope, its kind (sigstore or ed25519) and
who signed it. The installed version refers to a version of the same plugin.

The hub holds a plugin's bundle itself, outside any project, like a global skill's: blobs and blob_uploads take kind
plugin-bundle without a project. notifications.notice_kind takes 'plugin_disabled': the hub turned off an installed
plugin that no longer runs on its release.

Going back deletes the plugin_disabled notices, the hub's own plugin bundles and their uploads, puts the checks of 0033
back and drops the two tables, which is the schema that schema 0033 runs on; the bundles stay in the bucket.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

NOTICE_KINDS_0033 = (
    "push_default_branch",
    "merge_default_branch",
    "plan_finished",
    "run_failed",
    "curator_brief",
    "curator_paused",
    "author_waiting",
    "chat_mention",
    "chat_added",
)
PLUGIN_NOTICES = ("plugin_disabled",)
NOTICE_KINDS = (*NOTICE_KINDS_0033, *PLUGIN_NOTICES)  # evo_agents.hub.runs.NOTICE_KINDS
GLOBAL_KINDS_0033 = ("skill-bundle",)
GLOBAL_KINDS = (*GLOBAL_KINDS_0033, "plugin-bundle")  # evo_agents.hub.server.blobs.GLOBAL_KINDS
TIERS = ("isolated", "core")  # evo_agents.plugins.manifest.TIERS
SIGNATURE_KINDS = ("sigstore", "ed25519")  # evo_agents.plugins.signing.SIGNATURE_KINDS
NAME_PATTERN = "^[a-z][a-z0-9-]{0,62}$"  # a manifest's id
PUBLISHER_PATTERN = "^[a-z0-9][a-z0-9-]{0,62}$"  # a manifest's publisher
MAX_VERSION_CHARS = 60
MAX_REASON_CHARS = 2000
MAX_BUNDLE = 32 * 1024 * 1024  # evo_agents.plugins.bundle.MAX_BUNDLE
HUB_KIND_CHECKS = (("blobs", "blobs_hub_kind"), ("blob_uploads", "blob_uploads_hub_kind"))

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _create_plugins() -> None:
    op.create_table(
        "plugins",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("installed_version", sa.Text(), nullable=True),
        sa.Column("installed_by", sa.BigInteger(), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        _stamp("created_at"),
        sa.ForeignKeyConstraint(["installed_by"], ["users.id"], ondelete="RESTRICT", name="plugins_installed_by_fkey"),
        sa.PrimaryKeyConstraint("id", name="plugins_pkey"),
        sa.UniqueConstraint("name", name="plugins_name_key"),
        sa.CheckConstraint(col("name").regexp_match(NAME_PATTERN), name="plugins_name_check"),
        sa.CheckConstraint(col("publisher").regexp_match(PUBLISHER_PATTERN), name="plugins_publisher_check"),
        sa.CheckConstraint(
            sa.and_(
                col("installed_version").is_(None) == col("installed_by").is_(None),
                col("installed_version").is_(None) == col("installed_at").is_(None),
            ),
            name="plugins_installed_check",
        ),
        sa.CheckConstraint(
            sa.and_(
                col("disabled_reason").is_(None) == col("disabled_at").is_(None),
                sa.or_(col("disabled_reason").is_(None), col("installed_version").is_not(None)),
                sa.func.char_length(col("disabled_reason")) <= MAX_REASON_CHARS,
            ),
            name="plugins_disabled_check",
        ),
    )


def _create_versions() -> None:
    op.create_table(
        "plugin_versions",
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("r2_key", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signature", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signature_kind", sa.Text(), nullable=False),
        sa.Column("signed_by", sa.Text(), nullable=False),
        sa.Column("published_by", sa.BigInteger(), nullable=False),
        _stamp("published_at"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_versions_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["published_by"], ["users.id"], ondelete="RESTRICT", name="plugin_versions_published_by_fkey"
        ),
        sa.PrimaryKeyConstraint("plugin_id", "version", name="plugin_versions_pkey"),
        sa.CheckConstraint(
            sa.and_(
                sa.func.char_length(col("version")) <= MAX_VERSION_CHARS,
                col("sha256").regexp_match("^[0-9a-f]{64}$"),
                col("size").between(1, MAX_BUNDLE),
                col("tier").in_(TIERS),
                col("signature_kind").in_(SIGNATURE_KINDS),
            ),
            name="plugin_versions_check",
        ),
    )
    op.create_foreign_key(
        "plugins_installed_version_fkey",
        "plugins",
        "plugin_versions",
        ["id", "installed_version"],
        ["plugin_id", "version"],
        ondelete="RESTRICT",
    )


def _hub_kinds(kinds) -> None:
    for table, name in HUB_KIND_CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, sa.or_(col("project_id").is_not(None), col("kind").in_(kinds)))


def _notice_kinds(kinds) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    _create_plugins()
    _create_versions()
    _hub_kinds(GLOBAL_KINDS)
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind.in_(PLUGIN_NOTICES)))
    _notice_kinds(NOTICE_KINDS_0033)
    for table, _ in HUB_KIND_CHECKS:
        held = sa.table(table, col("project_id"), col("kind"))
        op.execute(held.delete().where(held.c.project_id.is_(None), held.c.kind == "plugin-bundle"))
    _hub_kinds(GLOBAL_KINDS_0033)
    op.drop_constraint("plugins_installed_version_fkey", "plugins", type_="foreignkey")
    op.drop_table("plugin_versions")
    op.drop_table("plugins")
