"""The proposals of @builder: the changes of settings its agent proposes, which an admin approves before the hub applies.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-18

The names below are copies of ``evo_agents.hub.tables_settings`` and ``evo_agents.hub.runs`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

builder_proposals holds each change the agent of a chat run of @builder (the core plugin builder, ``evo_agents.
core_plugins.builder``) proposed in a project: its kind (the branding of the hub, a layer of features, a layer of a
plugin's config, or an agent profile), where it applies (``target``), what it changes (``change``), its summary, the
version of the setting it was proposed on, the run, chat and thread it came from, and the hub's message that shows
it there. A proposal is pending until an admin approves or rejects it; approved, the hub applies it as the setting's
next version (``applied_version``), or keeps why it could not (``error``); an applied one an admin rolls back makes
the setting's next version a copy of the one before it (``rollback_version``). notifications.notice_kind takes
``builder_proposal``: a proposal waits for the member's approval.

Going back deletes the builder_proposal notices, drops the table and puts the check of 0042 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

NOTICE_KINDS_0042 = (
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
)
BUILDER_NOTICES = ("builder_proposal",)
NOTICE_KINDS = (*NOTICE_KINDS_0042, *BUILDER_NOTICES)  # evo_agents.hub.runs.NOTICE_KINDS
KINDS = ("branding", "features", "plugin_config", "agent")  # evo_agents.hub.tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # evo_agents.hub.tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000

col = sa.column


def _notice_kinds(kinds) -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def _foreign_keys() -> list:
    def key(column: str, table: str, *, ondelete: str):
        return sa.ForeignKeyConstraint(
            [column], [f"{table}.id"], ondelete=ondelete, name=f"builder_proposals_{column}_fkey"
        )

    return [
        key("project_id", "projects", ondelete="RESTRICT"),
        key("run_id", "runs", ondelete="SET NULL"),
        key("chat_id", "chats", ondelete="SET NULL"),
        key("thread_root_id", "chat_messages", ondelete="SET NULL"),
        key("message_id", "chat_messages", ondelete="SET NULL"),
        key("proposed_by", "users", ondelete="RESTRICT"),
        key("decided_by", "users", ondelete="RESTRICT"),
    ]


def upgrade() -> None:
    state = col("state")
    op.create_table(
        "builder_proposals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("thread_root_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("change", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("proposed_by", sa.BigInteger(), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_version", sa.Integer(), nullable=True),
        sa.Column("rollback_version", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_foreign_keys(),
        sa.PrimaryKeyConstraint("id", name="builder_proposals_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                state.in_(STATES),
                sa.func.char_length(col("summary")).between(1, MAX_SUMMARY_CHARS),
                sa.func.char_length(col("error")) <= MAX_ERROR_CHARS,
                col("base_version") >= 0,
                (state == "pending") == col("decided_at").is_(None),
                (state == "pending") == col("decided_by").is_(None),
                state.in_(("applied", "rolled_back")) == col("applied_version").is_not(None),
                (state == "rolled_back") == col("rollback_version").is_not(None),
            ),
            name="builder_proposals_check",
        ),
    )
    op.create_index("builder_proposals_project_idx", "builder_proposals", ["project_id", "id"])
    op.create_index("builder_proposals_run_idx", "builder_proposals", ["run_id"])
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.execute(notifications.delete().where(notifications.c.notice_kind.in_(BUILDER_NOTICES)))
    _notice_kinds(NOTICE_KINDS_0042)
    op.drop_index("builder_proposals_run_idx", table_name="builder_proposals")
    op.drop_index("builder_proposals_project_idx", table_name="builder_proposals")
    op.drop_table("builder_proposals")
