"""Command runs: a run of kind ``command``, the shell command line of a cron job a worker runs with no agent.

Revision ID: 0242
Revises: 0241
Create Date: 2026-11-12

Plan hub-cron-jobs, criterion a5 (``evo_agents.hub.command_runs``). runs_kind_check takes ``command``. A command run
names no plan (runs_kind_plan_check), may name no repo, as a task run (runs_kind_repos_check), and holds its command
line in ``request`` (runs_author_request_check, whose size check of 0020 bounds it as an author's request). A new check
holds its timeout to 240 minutes (runs_command_timeout_check). The kinds below are copies of those 0131, 0162, 0202 and
0100 left, never imports, so a later change takes a new revision.

Going back deletes the command runs, retries first, and puts the checks as they were back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None

KIND = "command"
KINDS_0131 = (  # runs.kind as 0131 left it, and no migration since changed it
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
)
ANY_PLAN_0162 = ("author", "local", "judge", "probe")  # the kinds that may name a plan or none, as 0162 left them
NO_PLAN_0131 = ("review", "chat", "task", "history", "release", "question", "browser")  # as 0131 left them
ANY_REPOS_0202 = ("chat", "local", "task", "history", "question", "browser", "review")  # as 0202 left them
ASKED_0100 = ("author", "task", "question")  # the kinds whose request is set, as 0100 left them
MAX_TIMEOUT_S = 240 * 60  # command_runs.MAX_TIMEOUT_MINUTES
CHECKS = ("runs_kind_check", "runs_kind_plan_check", "runs_kind_repos_check", "runs_author_request_check")
col = sa.column


def _checks(kinds, no_plan, any_repos, asked) -> None:
    kind = col("kind")
    rules = (
        kind.in_(kinds),
        sa.or_(kind.in_(ANY_PLAN_0162), kind.in_(no_plan) == col("plan_id").is_(None)),
        sa.or_(kind.in_(any_repos), (kind == "step") == col("repos").is_(None)),
        kind.in_(asked) == col("request").is_not(None),
    )
    for name, rule in zip(CHECKS, rules, strict=True):
        op.drop_constraint(name, "runs", type_="check")
        op.create_check_constraint(name, "runs", rule)


def upgrade() -> None:
    _checks((*KINDS_0131, KIND), (*NO_PLAN_0131, KIND), (*ANY_REPOS_0202, KIND), (*ASKED_0100, KIND))
    op.create_check_constraint(
        "runs_command_timeout_check", "runs", sa.or_(col("kind") != KIND, col("timeout_s") <= MAX_TIMEOUT_S)
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == KIND, runs.c.attempt == attempt))
    op.execute(runs.delete().where(runs.c.kind == KIND))
    op.drop_constraint("runs_command_timeout_check", "runs", type_="check")
    _checks(KINDS_0131, NO_PLAN_0131, ANY_REPOS_0202, ASKED_0100)
