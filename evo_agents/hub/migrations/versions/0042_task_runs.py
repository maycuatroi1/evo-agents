"""Agent types of plugins and task runs: the agent types projects enabled, and the run kind task a plugin dispatches.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-18

The names below are copies of ``evo_agents.hub.runs``, ``evo_agents.hub.task_runs`` and
``evo_agents.hub.plugin_events`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables_plugins`` describes
the tables.

project_agent_types holds each agent type of a plugin (``contributes.agent_types``) a project enabled: the plugin, the
type's name, the agent profile the hub made for it in the project, the member whose name its runs go under
(``run_as``), the names of that member's workers they may run on (none: any the profile allows), the cap of what its
runs may cost in 24 hours (``daily_usd``), who enabled it and when, and when it was turned off (``disabled_at``). A
project enables a type once: enabling it again changes that row.

runs takes the kind ``task``: the work a plugin hands an agent type of its own, with its text in ``request`` like an
author run's, no plan, and the repos of the project, none when it has none, like a chat run's. runs.agent_type_id names
the agent type of a task run and only of one; runs.pulls lists the draft pull requests the hub opened of its branch,
``{"repo", "url", "number"}`` each.
plugin_deliveries takes the kind ``task.result``, the event the plugin gets when a task run ends.

Going back deletes the task runs and the deliveries of task.result, drops the columns and the table, and puts the checks
of 0041 back, which is the schema that schema 0041 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

KINDS_0041 = ("step", "plan", "review", "judge", "author", "chat", "local")  # local: evo_agents.hub.runs.LOCAL_KIND
KINDS = (*KINDS_0041, "task")  # evo_agents.hub.runs.SHOWN_KINDS
DELIVERY_KINDS_0041 = ("run.state", "chat.message", "plan.step", "notification")
DELIVERY_KINDS = (*DELIVERY_KINDS_0041, "task.result")  # evo_agents.hub.plugin_events.KINDS
DELIVERY_STATES = ("pending", "delivered", "failed")  # evo_agents.hub.plugin_events.STATES
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"  # a contribution's name
MAX_ERROR_CHARS = 2000
MAX_DAILY_USD = 1000  # evo_agents.hub.task_runs.MAX_DAILY_USD
MAX_WORKERS = 16  # evo_agents.hub.task_runs.MAX_WORKERS
MAX_PULLS = 50  # a run's repos, at most

col = sa.column


def _replace(table: str, name: str, rule) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, rule)


def _checks(kinds, delivery_kinds, *, task: bool) -> None:
    """The checks that name the kinds of runs and of deliveries: those of 0041 without ``task``, else with it."""
    kind = col("kind")
    _replace("runs", "runs_kind_check", kind.in_(kinds))
    no_plan = kind.in_(("review", "chat", "task") if task else ("review", "chat")) == col("plan_id").is_(None)
    _replace("runs", "runs_kind_plan_check", sa.or_(kind.in_(("author", "local")), no_plan))
    asked = kind.in_(("author", "task")) if task else kind == "author"
    _replace("runs", "runs_author_request_check", asked == col("request").is_not(None))
    any_repos = kind.in_(("chat", "local", "task") if task else ("chat", "local"))  # a project may have no repo
    _replace("runs", "runs_kind_repos_check", sa.or_(any_repos, (kind == "step") == col("repos").is_(None)))
    _replace(
        "plugin_deliveries",
        "plugin_deliveries_check",
        sa.and_(
            kind.in_(delivery_kinds),
            col("contribution").regexp_match(NAME_PATTERN),
            col("state").in_(DELIVERY_STATES),
            col("attempts") >= 0,
            sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
            (col("state") == "delivered") == col("delivered_at").is_not(None),
        ),
    )


def _create_agent_types() -> None:
    op.create_table(
        "project_agent_types",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("run_as", sa.BigInteger(), nullable=False),
        sa.Column("workers", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("daily_usd", sa.Double(), nullable=False),
        sa.Column("enabled_by", sa.BigInteger(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="project_agent_types_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="project_agent_types_plugin_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="project_agent_types_profile_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_as"], ["users.id"], ondelete="RESTRICT", name="project_agent_types_run_as_fkey"),
        sa.ForeignKeyConstraint(
            ["enabled_by"], ["users.id"], ondelete="RESTRICT", name="project_agent_types_enabled_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="project_agent_types_pkey"),
        sa.UniqueConstraint("project_id", "plugin_id", "name", name="project_agent_types_name_key"),
        sa.CheckConstraint(
            sa.and_(
                col("name").regexp_match(NAME_PATTERN),
                col("daily_usd") > 0,
                col("daily_usd") <= MAX_DAILY_USD,
                sa.func.cardinality(col("workers")) <= MAX_WORKERS,
            ),
            name="project_agent_types_check",
        ),
    )
    op.create_index("project_agent_types_plugin_idx", "project_agent_types", ["plugin_id", "project_id"])


def upgrade() -> None:
    _create_agent_types()
    op.add_column("runs", sa.Column("agent_type_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("pulls", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_foreign_key(
        "runs_agent_type_id_fkey", "runs", "project_agent_types", ["agent_type_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_check_constraint("runs_task_check", "runs", (col("kind") == "task") == col("agent_type_id").is_not(None))
    op.create_check_constraint(
        "runs_pulls_check",
        "runs",
        sa.or_(
            col("pulls").is_(None),
            sa.case(  # the length of an array only: jsonb_array_length raises on anything else
                (sa.func.jsonb_typeof(col("pulls")) == "array", sa.func.jsonb_array_length(col("pulls")) <= MAX_PULLS),
                else_=sa.false(),
            ),
        ),
    )
    op.create_index(
        "runs_agent_type_idx",
        "runs",
        ["agent_type_id", "queued_at"],
        postgresql_where=col("agent_type_id").is_not(None),
    )
    _checks(KINDS, DELIVERY_KINDS, task=True)


def downgrade() -> None:
    deliveries = sa.table("plugin_deliveries", col("kind"))
    op.execute(deliveries.delete().where(deliveries.c.kind == "task.result"))
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == "task", runs.c.attempt == attempt))
    op.drop_index("runs_agent_type_idx", table_name="runs")
    op.drop_constraint("runs_pulls_check", "runs", type_="check")
    op.drop_constraint("runs_task_check", "runs", type_="check")
    op.drop_constraint("runs_agent_type_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "pulls")
    op.drop_column("runs", "agent_type_id")
    op.drop_table("project_agent_types")
    _checks(KINDS_0041, DELIVERY_KINDS_0041, task=False)
