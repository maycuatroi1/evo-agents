"""Probe runs: the run kind probe, and curator_probe_runs, each probe measured once a week on each model of the charter
(plan correction-recurrence, step 2).

Revision ID: 0131
Revises: 0130
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.runs``, ``evo_agents.hub.probes`` and ``evo_agents.hub.tables`` as of
this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation
on SQLAlchemy Core expressions.

runs takes the kind ``probe``: k new agent sessions with a change's rule on and k with it off, on the plan of the change
(plan_id set, as a replay run's), over its repos, queued by a schedule alone (runs_probe_schedule_check), one active per
project (runs_active_probe_key). curator_probe_runs holds one row per probe, ISO week (its Monday, in the charter's
time zone), runtime and model: the sessions each arm takes (k, as the charter said when it was queued), its run, its
state, the SHA-256 of the run's own key, the commits its arms ran from and
each session's result.

Going back drops the table, deletes the runs of kind probe and puts the checks of 0120 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

KIND = "probe"
KINDS_0120 = (  # runs.kind as 0120 set it, and no migration since changed it
    "step",
    "plan",
    "review",
    "judge",
    "author",
    "chat",
    "local",
    "task",
    "replay",
    "history",
    "release",
    "question",
    "browser",
)
KINDS = (*KINDS_0120, KIND)
ANY_PLAN = ("author", "local", "judge")  # the kinds that may name a plan or none, as of 0081
NO_PLAN = ("review", "chat", "task", "history", "release", "question", "browser")  # as 0120 set it; a probe has one
ANY_REPOS = ("chat", "local", "task", "history", "question", "browser")  # as 0120 set it; a probe run names its repos
ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")  # tables.RUN_ACTIVE
STATES = ("queued", "running", "done", "stale", "failed", "unmeasured")  # probes.RUN_STATES
MAX_REASON_CHARS = 2000
MAX_K = 10  # probes.MAX_K

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _replace(table: str, name: str, rule) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, rule)


def _checks(kinds) -> None:
    kind = col("kind")
    _replace("runs", "runs_kind_check", kind.in_(kinds))
    _replace("runs", "runs_kind_plan_check", sa.or_(kind.in_(ANY_PLAN), kind.in_(NO_PLAN) == col("plan_id").is_(None)))
    _replace("runs", "runs_kind_repos_check", sa.or_(kind.in_(ANY_REPOS), (kind == "step") == col("repos").is_(None)))


def upgrade() -> None:
    _checks(KINDS)
    op.create_check_constraint(
        "runs_probe_schedule_check", "runs", sa.or_(col("kind") != KIND, col("schedule_id").isnot(None))
    )
    op.create_index(
        "runs_active_probe_key",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == KIND, col("state").in_(ACTIVE)),
    )
    op.create_table(
        "curator_probe_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("probe_id", sa.BigInteger(), nullable=False),
        sa.Column("change_id", sa.BigInteger(), nullable=False),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("k", sa.SmallInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("probe_key", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("harness_sha", sa.Text(), nullable=True),
        sa.Column("sessions", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_probe_runs_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["probe_id"], ["curator_probes.id"], ondelete="CASCADE", name="curator_probe_runs_probe_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_probe_runs_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="curator_probe_runs_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="curator_probe_runs_pkey"),
        sa.UniqueConstraint(
            "probe_id",
            "week",
            "runtime",
            "model",
            name="curator_probe_runs_week_key",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            sa.and_(
                col("state").in_(STATES),
                sa.func.extract("isodow", col("week")) == 1,
                col("attempts") >= 0,
                col("k").between(1, MAX_K),
                sa.func.jsonb_typeof(col("sessions")) == "array",
                sa.func.coalesce(sa.func.length(col("reason")), 0) <= MAX_REASON_CHARS,
            ),
            name="curator_probe_runs_check",
        ),
    )
    op.create_index("curator_probe_runs_week_idx", "curator_probe_runs", ["project_id", "week", "id"])
    op.create_index("curator_probe_runs_run_idx", "curator_probe_runs", ["run_id"])


def downgrade() -> None:
    op.drop_index("curator_probe_runs_run_idx", table_name="curator_probe_runs")
    op.drop_index("curator_probe_runs_week_idx", table_name="curator_probe_runs")
    op.drop_table("curator_probe_runs")
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == KIND, runs.c.attempt == attempt))
    op.drop_index("runs_active_probe_key", table_name="runs")
    op.drop_constraint("runs_probe_schedule_check", "runs", type_="check")
    _checks(KINDS_0120)
