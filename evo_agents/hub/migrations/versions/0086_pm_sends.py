"""What the plugin pm sent, and the notices pm_daily and pm_escalation: pm_sends.

Revision ID: 0086
Revises: 0085
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_pm`` and ``evo_agents.hub.runs`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

pm_sends (plan pm-daily-tracking, criterion a6) holds one row for each thing pm sent a project on a day: the digest
(kind ``digest``, ``key`` empty) or the escalation of one blocker (kind ``escalation``, ``key`` names the blocker:
``blocker:<id>`` of a blocker set by hand, ``decision:<id>`` of an open decision). The unique key (project, day, kind,
key) is what lets the job run again, or twice, and send nothing more. The notice kinds ``pm_daily`` (the digest, in
the Inbox of the PM) and ``pm_escalation`` (a blocker to escalate, in the Inbox of the PM and of the one waited on)
join the check of notifications.notice_kind.

Going back deletes the notices of the two kinds, puts the check of 0084 back and drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None

TABLE = "pm_sends"
KINDS = ("digest", "escalation")  # evo_agents.hub.tables_pm.KINDS
NOTICES = ("pm_daily", "pm_escalation")  # evo_agents.hub.runs.NOTICE_KINDS
NOTICE_KINDS_0084 = (  # notifications_notice_kind_check as 0084 set it
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
NOTICE_KINDS = (*NOTICE_KINDS_0084, *NOTICES)  # evo_agents.hub.runs.NOTICE_KINDS

col = sa.column


def _notice_kinds(kinds) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), server_default="", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{TABLE}_project_id_fkey"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("project_id", "day", "kind", "key", name=f"{TABLE}_once_key"),
        sa.CheckConstraint(col("kind").in_(KINDS), name=f"{TABLE}_kind_check"),
    )
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind.in_(NOTICES)))
    _notice_kinds(NOTICE_KINDS_0084)
    op.drop_table(TABLE)
