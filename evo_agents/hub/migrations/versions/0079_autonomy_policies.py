"""The autonomy policy of a project, kept as revisions, and @builder's proposals of a change of it.

Revision ID: 0079
Revises: 0078
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_settings`` and ``evo_agents.hub.autonomy`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

autonomy_policies holds every revision of each project's autonomy policy (plan gitlab-merge-queue, criterion a2):
the project, the revision (1 and up within the project), the keys it sets (an object), the writer's note (at most
MAX_NOTE_CHARS), who wrote it and when. The hub only inserts: a project's policy is its highest revision, and a key a
revision leaves out reads as the default of the release that reads it. A project's revisions go with the project.

builder_proposals.kind takes ``policy``: @builder proposes a change of the project's policy, which an admin of the
project approves before the hub writes it as the next revision.

Going back deletes the proposals of kind policy and their Inbox notices, puts the check of 0043 back on
builder_proposals and drops autonomy_policies: the hub of 0078 has no policy, so every repo is off.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None

KINDS_0043 = ("branding", "features", "plugin_config", "agent")
POLICY = "policy"
KINDS = (*KINDS_0043, POLICY)  # evo_agents.hub.tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # evo_agents.hub.tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000
MAX_NOTE_CHARS = 1000  # evo_agents.hub.autonomy.MAX_NOTE_CHARS
PROPOSAL_CHECK = "builder_proposals_check"
NOTICE = "builder_proposal"

col = sa.column


def _proposal_check(kinds) -> None:
    """The check of builder_proposals as 0043 wrote it, its kind one of ``kinds``."""
    state = col("state")
    op.drop_constraint(PROPOSAL_CHECK, "builder_proposals", type_="check")
    op.create_check_constraint(
        PROPOSAL_CHECK,
        "builder_proposals",
        sa.and_(
            col("kind").in_(kinds),
            state.in_(STATES),
            sa.func.char_length(col("summary")).between(1, MAX_SUMMARY_CHARS),
            sa.func.char_length(col("error")) <= MAX_ERROR_CHARS,
            col("base_version") >= 0,
            (state == "pending") == col("decided_at").is_(None),
            (state == "pending") == col("decided_by").is_(None),
            state.in_(("applied", "rolled_back")) == col("applied_version").is_not(None),
            (state == "rolled_back") == col("rollback_version").is_not(None),
        ),
    )


def upgrade() -> None:
    body = col("body", postgresql.JSONB)
    op.create_table(
        "autonomy_policies",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="autonomy_policies_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT", name="autonomy_policies_created_by_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "revision", name="autonomy_policies_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("revision") >= 1,
                sa.func.jsonb_typeof(body) == "object",
                sa.func.char_length(col("note")).between(1, MAX_NOTE_CHARS),
            ),
            name="autonomy_policies_check",
        ),
    )
    _proposal_check(KINDS)


def downgrade() -> None:
    proposals = sa.table("builder_proposals", col("kind"))
    notifications = sa.table("notifications", col("notice_kind"), col("details", postgresql.JSONB))
    op.execute(
        notifications.delete().where(
            notifications.c.notice_kind == NOTICE, notifications.c.details["kind"].astext == POLICY
        )
    )
    op.execute(proposals.delete().where(proposals.c.kind == POLICY))
    _proposal_check(KINDS_0043)
    op.drop_table("autonomy_policies")
