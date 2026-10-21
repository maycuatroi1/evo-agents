"""The notice run_outputs of the files a run's agent sent, and the notices members turned off.

Revision ID: 0086
Revises: 0085
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.runs`` and ``evo_agents.hub.tables_outputs`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

notifications.notice_kind takes ``run_outputs``: the files a run's agent sent, or left in its outputs directory, which
the hub gathers a minute a run into one notice to the run's owner (plan run-outputs, criterion a6).
notification_mutes holds the notices each member turned off, one row a member and kind, and takes only the kinds a
member may turn off (MUTABLE_NOTICE_KINDS). No column of an older table changes.

Going back deletes the run_outputs notices, drops the table and puts the check of 0084 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None

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
NOTICE = "run_outputs"  # evo_agents.hub.runs.OUTPUT_NOTICE
NOTICE_KINDS = (*NOTICE_KINDS_0084, NOTICE)  # evo_agents.hub.runs.NOTICE_KINDS
MUTABLE_NOTICE_KINDS = (NOTICE,)  # evo_agents.hub.runs.MUTABLE_NOTICE_KINDS

col = sa.column


def _notice_kinds(kinds) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    op.create_table(
        "notification_mutes",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("notice_kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="notification_mutes_user_id_fkey"),
        sa.PrimaryKeyConstraint("user_id", "notice_kind", name="notification_mutes_pkey"),
        sa.CheckConstraint(col("notice_kind").in_(MUTABLE_NOTICE_KINDS), name="notification_mutes_check"),
    )
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    _notice_kinds(NOTICE_KINDS_0084)
    op.drop_table("notification_mutes")
