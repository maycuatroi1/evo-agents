"""The gates of autonomy: the decision and notice of a gate, and the environment label of a secret.

Revision ID: 0114
Revises: 0113
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.runs`` and ``evo_agents.hub.credentials`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

The hub holds a run to the modes of its principal at the lease, the merge, the deploy and the routes (plan
autonomy-levels, criterion a6): decisions.category takes ``autonomy``, what a gate asks the principal of a run whose
class is ask; notifications.notice_kind takes ``autonomy``, what a gate tells the principal of a class in notify.
secrets gains environment, the environment an env secret is labelled for (``test`` alone, or none): a run whose K7
is ``auto:test_secrets`` is leased the env secrets labelled ``test`` and no other that reaches an environment.

Going back deletes the decisions and notices of the kind, puts the checks of 0108 and 0095 back and drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

KIND = "autonomy"  # evo_agents.hub.runs.AUTONOMY_DECISION_CATEGORY and AUTONOMY_NOTICE
CATEGORIES_0095 = (  # decisions_category_check as 0095 set it
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
    "question",
    "triage",
    "release",
    "loop",
)
CATEGORIES = (*CATEGORIES_0095, KIND)  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES
NOTICE_KINDS_0108 = (  # notifications_notice_kind_check as 0108 set it
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
)
NOTICE_KINDS = (*NOTICE_KINDS_0108, KIND)  # evo_agents.hub.runs.NOTICE_KINDS
ENVIRONMENTS = ("test",)  # evo_agents.hub.credentials.SECRET_ENVIRONMENTS

col = sa.column


def _check(table: str, name: str, column: str, values) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, col(column).in_(values))


def upgrade() -> None:
    op.add_column("secrets", sa.Column("environment", sa.Text(), nullable=True))
    op.create_check_constraint("secrets_environment_check", "secrets", col("environment").in_(ENVIRONMENTS))
    _check("decisions", "decisions_category_check", "category", CATEGORIES)
    _check("notifications", "notifications_notice_kind_check", "notice_kind", NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    decisions = sa.table("decisions", col("category"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == KIND))
    op.execute(decisions.delete().where(decisions.c.category == KIND))  # with their notifications (on delete cascade)
    _check("notifications", "notifications_notice_kind_check", "notice_kind", NOTICE_KINDS_0108)
    _check("decisions", "decisions_category_check", "category", CATEGORIES_0095)
    op.drop_constraint("secrets_environment_check", "secrets", type_="check")
    op.drop_column("secrets", "environment")
