"""The decision the Loop asks a person, and its notice loop_paused.

Revision ID: 0089
Revises: 0088
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A work item of the Loop that failed a third time asks the person its last run stands for what next (plan loop-engine,
criterion a7): decisions.category takes ``loop``, a decision of the hub's own on that run, which the hub does as the
answer chooses (run it again, drop it, leave it to a person). When the Loop of a project makes more work items in an
hour than its policy lets it, its breaker trips and each admin of the project is told: notifications.notice_kind takes
``loop_paused``. No column of a table changes.

Going back deletes the decisions of category loop and the loop_paused notices, and puts the checks of 0088 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

CATEGORIES_0088 = (  # decisions_category_check as 0067 set it
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
)
CATEGORY = "loop"
CATEGORIES = (*CATEGORIES_0088, CATEGORY)  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES
NOTICE_KINDS_0088 = (  # notifications_notice_kind_check as 0084 set it
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
)
NOTICE = "loop_paused"
NOTICE_KINDS = (*NOTICE_KINDS_0088, NOTICE)  # evo_agents.hub.runs.NOTICE_KINDS

col = sa.column


def _check(table: str, name: str, column: str, values) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, col(column).in_(values))


def upgrade() -> None:
    _check("decisions", "decisions_category_check", "category", CATEGORIES)
    _check("notifications", "notifications_notice_kind_check", "notice_kind", NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    decisions = sa.table("decisions", col("category"))
    op.execute(decisions.delete().where(decisions.c.category == CATEGORY))
    _check("notifications", "notifications_notice_kind_check", "notice_kind", NOTICE_KINDS_0088)
    _check("decisions", "decisions_category_check", "category", CATEGORIES_0088)
