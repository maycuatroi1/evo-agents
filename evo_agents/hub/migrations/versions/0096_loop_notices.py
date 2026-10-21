"""The notices of the Loop's own clock: goal_review and loop_brief.

Revision ID: 0096
Revises: 0095
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A project whose Loop is on asks its admins and the owners of its active goals to review the goals on the first day of
each month (gate 1 of the decision board, plan loop-engine, criterion a8): notifications.notice_kind takes
``goal_review``. A project whose Loop is on and whose Curator sends no morning brief gets the Loop's part of it at
07:00, for the owners of its active goals: notifications.notice_kind takes ``loop_brief``. No column of a table changes.

Going back deletes the notices of both kinds and puts the check of 0095 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

NOTICE_KINDS_0095 = (  # notifications_notice_kind_check as 0095 set it
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
)
NOTICES = ("goal_review", "loop_brief")
NOTICE_KINDS = (*NOTICE_KINDS_0095, *NOTICES)  # evo_agents.hub.runs.NOTICE_KINDS

col = sa.column


def _check(values) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(values))


def upgrade() -> None:
    _check(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind.in_(NOTICES)))
    _check(NOTICE_KINDS_0095)
