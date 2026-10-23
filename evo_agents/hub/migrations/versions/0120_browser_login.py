"""Login sessions of browser identities: the run kind browser, the login URL, the notice browser_needs_login.

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.runs`` and ``evo_agents.hub.browser_login`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

runs takes the kind ``browser`` (plan agent-browser, criterion a7): a login session of a browser identity, run by a
person in the identity's Chromium with no agent. It is on no plan, has no request and no agent type, and its repos may
be NULL, and the owner's Save is its finish (runs_finish_check). browser_identities.login_url is the page it opens (NULL: the home page of the identity's first domain).
notifications.notice_kind takes ``browser_needs_login``, the notice an identity's member gets when it waits for a new
login.

Going back deletes the notices of the kind and the runs of kind browser (the identities they held let go of them), drops
the column and puts the checks of 0116 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

KIND = "browser"  # evo_agents.hub.browser_login.KIND
KINDS_0116 = (  # runs.kind as 0100 set it, and no migration since changed it
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
)
KINDS = (*KINDS_0116, KIND)
ANY_PLAN = ("author", "local", "judge")  # the kinds that may name a plan or none, as of 0081
NO_PLAN_0116 = ("review", "chat", "task", "history", "release", "question")
NO_PLAN = (*NO_PLAN_0116, KIND)
ANY_REPOS_0116 = ("chat", "local", "task", "history", "question")
ANY_REPOS = (*ANY_REPOS_0116, KIND)
FINISH_KINDS_0116 = ("author",)  # runs_finish_check as 0022 set it: whose owner ends the chat
FINISH_KINDS = (*FINISH_KINDS_0116, KIND)  # a login session ends with the owner's Save
NOTICE = "browser_needs_login"  # evo_agents.hub.browser_login.NEEDS_LOGIN_NOTICE
NOTICE_KINDS_0116 = (  # notifications_notice_kind_check as 0114 set it
    "push_default_branch",
    "merge_default_branch",
    "plan_finished",
    "run_failed",
    "curator_brief",
    "curator_paused",
    "author_waiting",
    "chat_mention",
    "chat_added",
    "plugin_disabled",
    "builder_proposal",
    "release",
    "canvas_baseline",
    "merge_paused",
    "pm_daily",
    "pm_escalation",
    "run_outputs",
    "loop_paused",
    "goal_review",
    "loop_brief",
    "plugin_notice",
    "worker_doctor",
    "autonomy",
)
NOTICE_KINDS = (*NOTICE_KINDS_0116, NOTICE)

col = sa.column


def _replace(table: str, name: str, rule) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, rule)


def _checks(kinds, no_plan, any_repos, finish_kinds, notice_kinds) -> None:
    kind = col("kind")
    _replace("runs", "runs_kind_check", kind.in_(kinds))
    _replace("runs", "runs_kind_plan_check", sa.or_(kind.in_(ANY_PLAN), kind.in_(no_plan) == col("plan_id").is_(None)))
    _replace("runs", "runs_kind_repos_check", sa.or_(kind.in_(any_repos), (kind == "step") == col("repos").is_(None)))
    _replace("runs", "runs_finish_check", sa.or_(col("finish_requested_at").is_(None), kind.in_(finish_kinds)))
    _replace("notifications", "notifications_notice_kind_check", col("notice_kind").in_(notice_kinds))


def upgrade() -> None:
    op.add_column("browser_identities", sa.Column("login_url", sa.Text(), nullable=True))
    _checks(KINDS, NO_PLAN, ANY_REPOS, FINISH_KINDS, NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    runs = sa.table("runs", col("kind"))
    op.execute(runs.delete().where(runs.c.kind == KIND))
    _checks(KINDS_0116, NO_PLAN_0116, ANY_REPOS_0116, FINISH_KINDS_0116, NOTICE_KINDS_0116)
    op.drop_column("browser_identities", "login_url")
