"""What Today keeps of a member, and the notice of their summary push (plan hub-mobile-phase-0, criterion a7).

Revision ID: 0151
Revises: 0150
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

today_marks holds one row a member Today knows of: ``seen_at``, the moment up to which the member saw what changed
(GET /v1/me/today counts from it; POST /v1/me/today/seen moves it forward only), and ``digest_on``, the local day of
the member's last summary push, so the job ``hub.today_digest`` sends at most one a day. A member's row goes with the
member, and a member without one saw nothing yet.

notifications.notice_kind takes ``today``, the summary push itself: a notice of no project that goes to the member's
push devices alone.

Going back deletes the notices of the kind, puts the check of 0144 back and drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None

TABLE = "today_marks"
NOTICE = "today"  # evo_agents.hub.runs.NOTICE_KINDS
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
NOTICE_KINDS = (*NOTICE_KINDS_0144, NOTICE)
CHECK = "notifications_notice_kind_check"

col = sa.column


def _check(kinds) -> None:
    op.drop_constraint(CHECK, "notifications", type_="check")
    op.create_check_constraint(CHECK, "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("digest_on", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name=f"{TABLE}_user_id_fkey"),
        sa.PrimaryKeyConstraint("user_id", name=f"{TABLE}_pkey"),
    )
    _check(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    _check(NOTICE_KINDS_0144)
    op.drop_table(TABLE)
