"""canvas, srs and tasks become features of the hub.

Revision ID: 0147
Revises: 0146
Create Date: 2026-10-25

plugins.builtin marks a plugin the hub registers itself as one of its features, not as a plugin of the store: true for
canvas, srs and tasks, false for every other. A project has those on through its features (hub_settings, kind
features), no longer through project_plugins: for each project that enabled one or more of them, this revision writes
one new version of the project's layer of its features, a copy of its current values (none when it had no layer) with
each of them true, written by whoever enabled the first of them, when they did (or when the version before was
written, were that later): the feature is on since then. The rows of project_plugins of the three go to
retired_project_plugins as they were, with the version written (``settings_version``), and leave project_plugins. The
configs (plugin_configs), agent types (project_agent_types) and runs of the three keep their plugin and stay.

Going back puts each row of retired_project_plugins back in project_plugins with the permissions it had, deletes the
versions of the features this revision wrote, takes the keys canvas, srs and tasks out of every version of the
features (0146 knows none of them), and drops retired_project_plugins and plugins.builtin. A project that turned one
of them on through its features after this revision has it off below 0147, where only project_plugins says so.

The names below are a copy of ``evo_agents.hub.plugin_calls.BUILTIN`` as of this revision, never an import.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None

BUILTIN = ("canvas", "srs", "tasks")  # evo_agents.hub.plugin_calls.BUILTIN
FEATURES = "features"  # hub_settings.kind
RETIRED = "retired_project_plugins"

col = sa.column
COLUMNS = ("project_id", "plugin_id", "permissions", "enabled_by", "enabled_at", "granted_by", "granted_at")


def _tables():
    plugins = sa.table("plugins", col("id"), col("name"), col("builtin"))
    pp = sa.table(
        "project_plugins",
        *(col(name) for name in COLUMNS[:2]),
        col("permissions", postgresql.ARRAY(sa.Text)),
        *(col(name) for name in COLUMNS[3:]),
    )
    retired = sa.table(
        RETIRED,
        *(col(name) for name in COLUMNS[:2]),
        col("permissions", postgresql.ARRAY(sa.Text)),
        *(col(name) for name in COLUMNS[3:]),
        col("settings_version"),
    )
    settings = sa.table(
        "hub_settings",
        col("kind"),
        col("project_id"),
        col("version"),
        col("values", postgresql.JSONB),
        col("changed_by"),
        col("changed_at"),
    )
    return plugins, pp, retired, settings


def upgrade() -> None:
    op.add_column("plugins", sa.Column("builtin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        RETIRED,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("enabled_by", sa.BigInteger(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by", sa.BigInteger(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="retired_project_plugins_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="retired_project_plugins_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["enabled_by"], ["users.id"], ondelete="RESTRICT", name="retired_project_plugins_enabled_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"], ["users.id"], ondelete="RESTRICT", name="retired_project_plugins_granted_by_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "plugin_id", name="retired_project_plugins_pkey"),
    )
    plugins, pp, retired, settings = _tables()
    conn = op.get_bind()
    conn.execute(sa.update(plugins).where(plugins.c.name.in_(BUILTIN)).values(builtin=True))
    rows = conn.execute(
        sa.select(*(pp.c[name] for name in COLUMNS), plugins.c.name)
        .join_from(pp, plugins, plugins.c.id == pp.c.plugin_id)
        .where(plugins.c.builtin)
        .order_by(pp.c.project_id, pp.c.enabled_at, plugins.c.name)
    ).all()
    by_project: dict[int, list] = {}
    for row in rows:
        by_project.setdefault(row.project_id, []).append(row)
    for project_id, enabled in by_project.items():
        current = conn.execute(
            sa.select(settings.c.version, settings.c["values"], settings.c.changed_at)
            .where(settings.c.kind == FEATURES, settings.c.project_id == project_id)
            .order_by(settings.c.version.desc())
            .limit(1)
        ).first()
        values = dict(current[1]) if current else {}
        values.update({row.name: True for row in enabled})
        version = (current[0] if current else 0) + 1
        since = max(enabled[0].enabled_at, current[2]) if current else enabled[0].enabled_at
        written = {"version": version, "values": values, "changed_by": enabled[0].enabled_by, "changed_at": since}
        conn.execute(sa.insert(settings).values(kind=FEATURES, project_id=project_id, **written))
        for row in enabled:
            conn.execute(
                sa.insert(retired).values(**{name: getattr(row, name) for name in COLUMNS}, settings_version=version)
            )
    builtin = sa.select(plugins.c.id).where(plugins.c.builtin).scalar_subquery()
    conn.execute(sa.delete(pp).where(pp.c.plugin_id.in_(builtin)))


def downgrade() -> None:
    plugins, pp, retired, settings = _tables()
    conn = op.get_bind()
    conn.execute(sa.insert(pp).from_select(list(COLUMNS), sa.select(*(retired.c[name] for name in COLUMNS))))
    written = sa.select(retired.c.project_id, retired.c.settings_version).where(retired.c.settings_version.is_not(None))
    for project_id, version in set(conn.execute(written).all()):
        conn.execute(
            sa.delete(settings).where(
                settings.c.kind == FEATURES, settings.c.project_id == project_id, settings.c.version == version
            )
        )
    stripped = settings.c["values"]
    for name in BUILTIN:
        stripped = stripped.op("-")(sa.literal(name, sa.Text))
    conn.execute(sa.update(settings).where(settings.c.kind == FEATURES).values({"values": stripped}))
    op.drop_table(RETIRED)
    op.drop_column("plugins", "builtin")
