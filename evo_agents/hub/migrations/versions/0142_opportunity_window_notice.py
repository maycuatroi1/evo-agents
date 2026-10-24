"""The notice opportunity_window: an open opportunity's window closes (plan opportunity-intake, step 3).

Revision ID: 0142
Revises: 0141
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

notifications.notice_kind takes ``opportunity_window``, which the hub's job ``hub.opportunity_deadlines`` sends to the
owner of an open opportunity at 14 and at 3 days before its window ends and the day after, each once per window date.
Nothing else changes: the hub keeps no mark of what it sent besides the notice itself.

Going back deletes the notices of the kind and puts the check of 0132 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None

NOTICE = "opportunity_window"  # evo_agents.hub.runs.NOTICE_KINDS
NOTICE_KINDS_0132 = (  # notifications_notice_kind_check as 0132 set it, and no migration since changed it
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
    "browser_needs_login",
    "curator_probe_report",
)
NOTICE_KINDS = (*NOTICE_KINDS_0132, NOTICE)
CHECK = "notifications_notice_kind_check"

col = sa.column


def _check(kinds) -> None:
    op.drop_constraint(CHECK, "notifications", type_="check")
    op.create_check_constraint(CHECK, "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    _check(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    _check(NOTICE_KINDS_0132)
