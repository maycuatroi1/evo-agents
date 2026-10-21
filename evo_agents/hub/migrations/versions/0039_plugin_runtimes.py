"""Runtimes of plugins: a run's runtime is any registered one, and a worker names the plugins whose runtimes it takes.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-18

The bounds below are copies of ``evo_agents.hub.runs`` and ``evo_agents.plugins.manifest`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions, as ``evo_agents.hub.tables`` describes the tables.

Schema 0009 held runs.requested_runtime and runs.runtime to 'any' and the three runtimes of the core. A plugin may
now declare a runtime, registered as <plugin>/<name> in the projects that enabled it: the hub checks a dispatch
against the runtimes registered in the run's project (``evo_agents.hub.server.runs.service.plugin_runtimes``), and
the two CHECKs take any, the core's three and every name of that shape (RUNTIME_NAME). The constraints keep the
names Postgres gave them in 0009.

workers.plugins lists the plugins whose runtimes the worker takes, as its owner set them (PATCH /v1/workers/{id}):
at most MAX_WORKER_PLUGINS ids of plugins, each once is the api's rule; empty by default, so a worker takes no runtime
of a plugin until its owner says so.

Going back drops workers.plugins and puts the CHECKs of 0009 back, which is the schema that schema 0038 runs on. The
runs of plugins' runtimes cannot be held there, so it deletes them first (their events, inbox, decisions and leases go
with them): each one no other run names as the run it retries, reruns or goes on from, until none is left; a run of
the core that names one would stop it, which never happens, since a retry, a rerun and a resumed run keep their
runtime.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

RUNTIMES_0009 = ("any", "claude-code", "opencode", "codex")
RUNTIME_NAME = "^(any|claude-code|opencode|codex|[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9_-]{0,62})$"  # runs.RUNTIME_NAME
PLUGIN_ID = "[a-z][a-z0-9-]{0,62}"  # evo_agents.plugins.manifest.PLUGIN_ID, without its anchors
MAX_WORKER_PLUGINS = 16  # evo_agents.hub.runs.MAX_WORKER_PLUGINS
# Every element an id of a plugin, matched on the array as JSON, as 0009 matches the labels of a worker.
PLUGINS_JSON = rf'^\[("{PLUGIN_ID}"(,"{PLUGIN_ID}")*)?\]$'
COLUMNS = ("requested_runtime", "runtime")

col = sa.column


def _runtime_checks(rule) -> None:
    for name in COLUMNS:
        op.drop_constraint(f"runs_{name}_check", "runs", type_="check")
        op.create_check_constraint(f"runs_{name}_check", "runs", rule(col(name)))


def upgrade() -> None:
    _runtime_checks(lambda column: column.regexp_match(RUNTIME_NAME))
    op.add_column(
        "workers",
        sa.Column("plugins", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
    )
    op.create_check_constraint(
        "workers_plugins_check",
        "workers",
        sa.and_(
            sa.func.cardinality(col("plugins")) <= MAX_WORKER_PLUGINS,
            sa.cast(sa.func.array_to_json(col("plugins")), sa.Text()).regexp_match(PLUGINS_JSON),
        ),
    )


def downgrade() -> None:
    runs = sa.table("runs", col("id"), col("requested_runtime"), col("parent_run_id"), col("resume_of_run_id"))
    child = runs.alias("child")
    of_plugin = runs.c.requested_runtime.not_in(RUNTIMES_0009)
    named = sa.exists().where(sa.or_(child.c.parent_run_id == runs.c.id, child.c.resume_of_run_id == runs.c.id))
    conn = op.get_bind()
    while conn.execute(runs.delete().where(of_plugin, ~named)).rowcount:
        pass
    left = conn.execute(sa.select(runs.c.id).where(of_plugin).order_by(runs.c.id)).scalars().all()
    if left:
        raise RuntimeError(
            f"schema 0038 holds no run of a plugin's runtime, and run {', '.join(map(str, left))} cannot be deleted: a "
            "run of the core names it as the run it retries, reruns or goes on from"
        )
    op.drop_constraint("workers_plugins_check", "workers", type_="check")
    op.drop_column("workers", "plugins")
    _runtime_checks(lambda column: column.in_(RUNTIMES_0009))
