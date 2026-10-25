"""What Today keeps of a member, and the notice of their summary push (plan hub-mobile-phase-0, criterion a7).

Revision ID: 0158
Revises: 0157
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

today_marks holds one row a member Today knows of: ``seen_at``, the moment up to which the member saw what changed
(GET /v1/me/today counts from it; POST /v1/me/today/seen moves it forward only), and ``digest_on``, the local day of
the member's last summary push, so the job ``hub.today_digest`` sends at most one a day. A member's row goes with the
member, and a member without one saw nothing yet.

notifications.notice_kind takes ``today``, the summary push itself: a notice of no project that goes to the member's
push devices alone. The check keeps what 0153 made of it, one of the hub's kinds as of 0144 or a name of a core
plugin's kind (NAME), and adds ``today``, which has no ``_`` and so is no plugin's name.

Going back deletes the notices of the kind, puts the check of 0153 back and drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None

TABLE = "today_marks"
NOTICE = "today"  # evo_agents.hub.runs.NOTICE_KINDS
NOTICE_KINDS_0144 = (  # the hub's kinds as 0144 set them, which the check of 0153 keeps beside NAME
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
NAME = "^[a-z][a-z0-9]*_[a-z0-9_]+$"  # a core plugin's kind of notification, its table prefix first (0153)
CHECK = "notifications_notice_kind_check"

col = sa.column


def _check(kinds) -> None:
    """notifications_notice_kind_check as 0153 shaped it: one of ``kinds`` or a name of a core plugin's kind."""
    op.drop_constraint(CHECK, "notifications", type_="check")
    kind = col("notice_kind")
    op.create_check_constraint(CHECK, "notifications", sa.or_(kind.in_(kinds), kind.regexp_match(NAME)))


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
