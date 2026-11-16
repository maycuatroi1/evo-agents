"""Evolve runs: a run of kind ``evolve``, the Curator's evolution of a harness or an agent's configuration.

Revision ID: 0257
Revises: 0256
Create Date: 2026-11-16

Plan pareto-curator-evolution, step 2, criterion a2 (``evo_agents.hub.server.evolve``). runs_kind_check takes
``evolve``. An evolve run names no plan (runs_kind_plan_check) and may name no repo, as a task run does
(runs_kind_repos_check): an evolution of an agent's configuration works in no repo. runs_active_evolve_key keeps one
evolve run of a project active at a time, as runs_active_probe_key does. The kinds below are copies of those 0244 left,
never imports, so a later change takes a new revision.

Going back deletes the evolve runs, retries first, drops the index and puts the checks as they were back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0257"
down_revision = "0256"
branch_labels = None
depends_on = None

KIND = "evolve"
KINDS_0244 = (  # runs.kind as 0244 left it, and no migration since changed it
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
    "probe",
    "command",
)
ANY_PLAN_0162 = ("author", "local", "judge", "probe")  # the kinds that may name a plan or none, as 0162 left them
NO_PLAN_0244 = ("review", "chat", "task", "history", "release", "question", "browser", "command")  # as 0244 left them
ANY_REPOS_0244 = ("chat", "local", "task", "history", "question", "browser", "review", "command")  # as 0244 left them
ASKED_0244 = ("author", "task", "question", "command")  # the kinds whose request is set, as 0244 left them
ACTIVE = (
    "queued",
    "leased",
    "running",
    "interactive",
    "verifying",
    "waiting",
    "review",
    "parked",
)  # runs.ACTIVE_STATES
CHECKS = ("runs_kind_check", "runs_kind_plan_check", "runs_kind_repos_check")
INDEX = "runs_active_evolve_key"
col = sa.column


def _checks(kinds, no_plan, any_repos) -> None:
    kind = col("kind")
    rules = (
        kind.in_(kinds),
        sa.or_(kind.in_(ANY_PLAN_0162), kind.in_(no_plan) == col("plan_id").is_(None)),
        sa.or_(kind.in_(any_repos), (kind == "step") == col("repos").is_(None)),
    )
    for name, rule in zip(CHECKS, rules, strict=True):
        op.drop_constraint(name, "runs", type_="check")
        op.create_check_constraint(name, "runs", rule)


def upgrade() -> None:
    _checks((*KINDS_0244, KIND), (*NO_PLAN_0244, KIND), (*ANY_REPOS_0244, KIND))
    op.create_index(
        INDEX,
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == KIND, col("state").in_(ACTIVE)),
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == KIND, runs.c.attempt == attempt))
    op.execute(runs.delete().where(runs.c.kind == KIND))
    op.drop_index(INDEX, table_name="runs")
    _checks(KINDS_0244, NO_PLAN_0244, ANY_REPOS_0244)
