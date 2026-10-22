"""The notice an isolated plugin sends: plugin_notice, and the keys that make a send happen once.

Revision ID: 0097
Revises: 0096
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_plugin_notices`` and ``evo_agents.hub.runs`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

An isolated plugin the project granted ``notifications:send`` sends the project's members a notice through
``POST /v1/plugin-api/projects/{p}/notifications`` (plan plugin-notify-pm-workdays, criterion a1): notifications.
notice_kind takes ``plugin_notice``, one kind for every plugin, whose details name the plugin, version, topic and key.
plugin_notice_keys holds one row for each (project, plugin, key) that was sent, with the answer of the send: a second
send with the same key creates nothing and answers the first one's result.

Going back deletes the notices of the kind, puts the check of 0096 back and drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

TABLE = "plugin_notice_keys"
NOTICE = "plugin_notice"  # evo_agents.hub.runs.NOTICE_KINDS
NOTICE_KINDS_0096 = (  # notifications_notice_kind_check as 0096 set it
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
)
NOTICE_KINDS = (*NOTICE_KINDS_0096, NOTICE)  # evo_agents.hub.runs.NOTICE_KINDS

col = sa.column


def _check(values) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(values))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{TABLE}_project_id_fkey"),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="CASCADE", name=f"{TABLE}_plugin_id_fkey"),
        sa.PrimaryKeyConstraint("project_id", "plugin_id", "key", name=f"{TABLE}_pkey"),
    )
    _check(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    _check(NOTICE_KINDS_0096)
    op.drop_table(TABLE)
