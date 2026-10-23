"""The notice curator_probe_report: the weekly report of a project's probes (plan correction-recurrence, step 3).

Revision ID: 0132
Revises: 0131
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

notifications.notice_kind takes ``curator_probe_report``, which the hub sends on Monday at the charter's brief_at to the
owner of the night shift's schedule of a project whose charter runs probes.

Going back deletes the notices of the kind and puts the check of 0120 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None

NOTICE = "curator_probe_report"  # evo_agents.hub.runs.PROBE_REPORT_NOTICE
NOTICE_KINDS_0120 = (  # notifications_notice_kind_check as 0120 set it, and no migration since changed it
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
)
NOTICE_KINDS = (*NOTICE_KINDS_0120, NOTICE)
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
    _check(NOTICE_KINDS_0120)
