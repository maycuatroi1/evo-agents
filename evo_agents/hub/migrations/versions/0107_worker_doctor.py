"""The newest doctor report of each worker, and the notice worker_doctor.

Revision ID: 0107
Revises: 0106
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_workers`` and ``evo_agents.hub.runs`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

A daemon that says the feature doctor_report runs ``evo-agents worker doctor`` when it starts and every 24 hours and
sends what it found (plan credential-sharing, criterion a6; ``evo_agents.hub.server.worker_doctor``). workers keeps the
newest report: doctor_at, when the hub took it; doctor_high and doctor_medium, how many of its findings are of each
severity; doctor_findings, the findings themselves (code, severity, detail). A worker narrowed to units of the org
chart takes the runs of their members only while that report has no high finding, and a report with a high one sends
its owner the notice ``worker_doctor``, which notifications.notice_kind takes from here on.

Going back deletes the notices of the kind, puts the check of 0097 back and drops the columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

WORKERS = "workers"
COLUMNS = ("doctor_at", "doctor_high", "doctor_medium", "doctor_findings")
NOTICE = "worker_doctor"  # evo_agents.hub.runs.NOTICE_KINDS
NOTICE_KINDS_0097 = (  # notifications_notice_kind_check as 0097 set it
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
)
NOTICE_KINDS = (*NOTICE_KINDS_0097, NOTICE)  # evo_agents.hub.runs.NOTICE_KINDS

col = sa.column


def _check(values) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(values))


def upgrade() -> None:
    op.add_column(WORKERS, sa.Column("doctor_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(WORKERS, sa.Column("doctor_high", sa.SmallInteger(), nullable=True))
    op.add_column(WORKERS, sa.Column("doctor_medium", sa.SmallInteger(), nullable=True))
    op.add_column(WORKERS, sa.Column("doctor_findings", postgresql.JSONB(), nullable=True))
    _check(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    _check(NOTICE_KINDS_0097)
    for name in reversed(COLUMNS):
        op.drop_column(WORKERS, name)
