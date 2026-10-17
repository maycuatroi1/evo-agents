"""Plugin extension points: the deliveries of events and notifications to plugins, and the core plugins that come with
the hub's image.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-17

``evo_agents.hub.tables_plugins`` describes the tables; the lists below are copies of it, of
``evo_agents.hub.plugin_events`` and of ``evo_agents.plugins.signing`` as of this revision, never imports, so a later
change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

plugin_deliveries is the outbox of the plugins' events: one row for one event (a run's state, a chat message, a plan
step) or one notification, for one plugin enabled in the event's project, written in the transaction that made it.
The job hub.deliver_plugin_events hands each to its plugin, at least once: a row is delivered only once the plugin
answered, and a failed try is tried again later, up to five times. Its kind, the name of the plugin's contribution it
goes to, its payload, its state (pending, delivered, failed), how many tries, when it is due, its last error, and when
it was made and delivered.

A core plugin that comes with the hub's image is recorded in the store like a published one, so projects enable it,
grant it what it asks for and configure it: its version's signature kind is 'image', which nobody published
(published_by is NULL), and nobody installed it (installed_by may be NULL while it is installed).

Going back deletes the deliveries, then the plugins of the image with their versions, the projects' grants and
configs and the run records of them, and puts the checks of 0033 back, which is the schema that schema 0036 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

TIERS = ("isolated", "core")  # evo_agents.plugins.manifest.TIERS
SIGNATURE_KINDS_0033 = ("sigstore", "ed25519")  # evo_agents.plugins.signing.SIGNATURE_KINDS
IMAGE = "image"  # evo_agents.hub.server.plugin_store.IMAGE
SIGNATURE_KINDS = (*SIGNATURE_KINDS_0033, IMAGE)
KINDS = ("run.state", "chat.message", "plan.step", "notification")  # evo_agents.hub.plugin_events.KINDS
STATES = ("pending", "delivered", "failed")  # evo_agents.hub.plugin_events.STATES
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"  # a contribution's name
MAX_VERSION_CHARS = 60
MAX_BUNDLE = 32 * 1024 * 1024  # evo_agents.plugins.bundle.MAX_BUNDLE
MAX_ERROR_CHARS = 2000

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _versions_check(kinds) -> None:
    op.drop_constraint("plugin_versions_check", "plugin_versions", type_="check")
    op.create_check_constraint(
        "plugin_versions_check",
        "plugin_versions",
        sa.and_(
            sa.func.char_length(col("version")) <= MAX_VERSION_CHARS,
            col("sha256").regexp_match("^[0-9a-f]{64}$"),
            col("size").between(1, MAX_BUNDLE),
            col("tier").in_(TIERS),
            col("signature_kind").in_(kinds),
        ),
    )


def _installed_check(image: bool) -> None:
    op.drop_constraint("plugins_installed_check", "plugins", type_="check")
    installed_by = (
        sa.or_(col("installed_by").is_(None), col("installed_version").is_not(None))
        if image
        else col("installed_version").is_(None) == col("installed_by").is_(None)
    )
    op.create_check_constraint(
        "plugins_installed_check",
        "plugins",
        sa.and_(installed_by, col("installed_version").is_(None) == col("installed_at").is_(None)),
    )


def _create_deliveries() -> None:
    op.create_table(
        "plugin_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("contribution", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        _stamp("next_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        _stamp("created_at"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_deliveries_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="plugin_deliveries_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="plugin_deliveries_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                col("contribution").regexp_match(NAME_PATTERN),
                col("state").in_(STATES),
                col("attempts") >= 0,
                sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
                (col("state") == "delivered") == col("delivered_at").is_not(None),
            ),
            name="plugin_deliveries_check",
        ),
    )
    op.create_index(
        "plugin_deliveries_due_idx",
        "plugin_deliveries",
        ["next_at", "id"],
        postgresql_where=col("state") == "pending",
    )
    op.create_index("plugin_deliveries_plugin_idx", "plugin_deliveries", ["plugin_id", "id"])
    op.create_index("plugin_deliveries_created_idx", "plugin_deliveries", ["created_at"])


def upgrade() -> None:
    _versions_check(SIGNATURE_KINDS)
    op.alter_column("plugin_versions", "published_by", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        "plugin_versions_image_check",
        "plugin_versions",
        (col("signature_kind") == IMAGE) == col("published_by").is_(None),
    )
    _installed_check(image=True)
    _create_deliveries()


def downgrade() -> None:
    op.drop_table("plugin_deliveries")
    versions = sa.table("plugin_versions", col("plugin_id"), col("version"), col("signature_kind"))
    image = sa.select(versions.c.plugin_id).where(versions.c.signature_kind == IMAGE)
    runs = sa.table("run_plugins", col("plugin_id"))
    op.execute(runs.delete().where(runs.c.plugin_id.in_(image)))
    cleared = ("installed_version", "installed_by", "installed_at", "disabled_reason", "disabled_at")
    plugins = sa.table("plugins", col("id"), *(col(name) for name in cleared))
    op.execute(plugins.update().where(plugins.c.id.in_(image)).values(dict.fromkeys(cleared)))
    op.execute(plugins.delete().where(plugins.c.id.in_(image)))  # versions, grants, configs and tokens go with it
    _installed_check(image=False)
    op.drop_constraint("plugin_versions_image_check", "plugin_versions", type_="check")
    op.alter_column("plugin_versions", "published_by", existing_type=sa.BigInteger(), nullable=False)
    _versions_check(SIGNATURE_KINDS_0033)
