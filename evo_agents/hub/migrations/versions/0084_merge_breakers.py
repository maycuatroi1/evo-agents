"""The breaker of each repo of the hub's merge queue, and its notice merge_paused.

Revision ID: 0084
Revises: 0083
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_merge``, ``evo_agents.hub.merge_breaker`` and
``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

Each repo of a project the merge queue passes over has a breaker (plan gitlab-merge-queue, criterion a7): armed, or
tripped by a failed check, a rollback, a revert that cannot go on, a target branch red too long, or too many reverts
or passes of the Judge found wrong. merge_breakers holds one row per project and repo: its state (STATES), what
tripped it last (CAUSES) and why, when, how often, who reset it last and when, and since when the hub has seen its
target branch red. A tripped breaker names its cause, its reason and when it tripped. notifications.notice_kind takes
``merge_paused``: the breaker of a repo tripped and the queue of that repo waits for an admin. No column of an older
table changes.

Going back deletes the merge_paused notices, puts the merge requests the breaker paused back in line (``ready``),
drops the table and puts the check of 0077 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

NOTICE_KINDS_0077 = (  # notifications_notice_kind_check as 0077 set it
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
)
NOTICE = "merge_paused"
NOTICE_KINDS = (*NOTICE_KINDS_0077, NOTICE)  # evo_agents.hub.runs.NOTICE_KINDS
STATES = ("armed", "tripped")  # evo_agents.hub.merge_breaker.STATES
CAUSES = (  # evo_agents.hub.merge_breaker.CAUSES
    "uat_check",
    "prod_check",
    "rollback",
    "revert",
    "target_red",
    "reverts_in_a_row",
    "revert_rate",
    "judge_misses",
)
MAX_REASON_CHARS = 2000  # evo_agents.hub.merge_breaker.MAX_REASON_CHARS

col = sa.column


def _notice_kinds(kinds) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    state = col("state")
    op.create_table(
        "merge_breakers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="armed", nullable=False),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _when("tripped_at"),
        sa.Column("trips", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resumed_by", sa.BigInteger(), nullable=True),
        _when("resumed_at"),
        _when("red_since"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="merge_breakers_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["resumed_by"], ["users.id"], ondelete="SET NULL", name="merge_breakers_resumed_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="merge_breakers_pkey"),
        sa.UniqueConstraint("project_id", "repo", name="merge_breakers_project_id_repo_key"),
        sa.CheckConstraint(
            sa.and_(
                state.in_(STATES),
                col("cause").in_(CAUSES),
                sa.func.char_length(col("reason")).between(1, MAX_REASON_CHARS),
                col("trips") >= 0,
                sa.or_(
                    state != "tripped",
                    sa.and_(col("cause").is_not(None), col("reason").is_not(None), col("tripped_at").is_not(None)),
                ),
            ),
            name="merge_breakers_check",
        ),
    )
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind == NOTICE))
    _notice_kinds(NOTICE_KINDS_0077)
    queue = sa.table("merge_queue", col("state"), col("reason"))
    op.execute(queue.update().values(state="ready", reason=None).where(queue.c.state == "paused"))
    op.drop_table("merge_breakers")
