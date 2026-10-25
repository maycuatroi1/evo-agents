"""Core plugins add kinds of notification: notifications.notice_kind takes the hub's kinds and any name of a plugin's.

Revision ID: 0148
Revises: 0147
Create Date: 2026-10-25

A core plugin declares the kinds of notification it sends in its manifest (extension point ``notice_kinds``), each
named with the plugin's table prefix (``northwind_ledger_overdue``), and sends them through the core plugin API
(``core_plugin_api.notify``), which checks the kind against the manifest of the installed plugin. The check of the
column keeps the shape only: one of the hub's kinds as of 0144, or a name of the form NAME, a lower-case word, ``_``,
then lower-case letters, digits and ``_``. The names below are copies of ``evo_agents.hub.runs`` as of this revision,
never imports. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back deletes the notices of every kind the hub's list lacks and puts the check of 0144 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None

NOTICE_KINDS_0144 = (  # notifications_notice_kind_check as 0144 set it, and no migration since changed it
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
    "opportunity_window",
)
NAME = "^[a-z][a-z0-9]*_[a-z0-9_]+$"  # a plugin's kind of notification, its table prefix first
CHECK = "notifications_notice_kind_check"
col = sa.column


def upgrade() -> None:
    op.drop_constraint(CHECK, "notifications", type_="check")
    kind = col("notice_kind")
    op.create_check_constraint(CHECK, "notifications", sa.or_(kind.in_(NOTICE_KINDS_0144), kind.regexp_match(NAME)))


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    kind = notifications.c.notice_kind
    op.execute(notifications.delete().where(kind.is_not(None), kind.not_in(NOTICE_KINDS_0144)))
    op.drop_constraint(CHECK, "notifications", type_="check")
    op.create_check_constraint(CHECK, "notifications", col("notice_kind").in_(NOTICE_KINDS_0144))
