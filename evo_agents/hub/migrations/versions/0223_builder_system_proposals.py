"""@builder's proposals of an agent system: builder_proposals.kind takes ``system``.

Revision ID: 0223
Revises: 0221
Create Date: 2026-11-07

The names below are copies of ``evo_agents.hub.tables_settings`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A proposal of kind ``system`` (plan multi-agent-systems, criterion a11) holds the spec ``evo-agent-system/1`` an
agent system of the project would have, with the agents and named graphs it needs that the project does not have yet;
an admin of the project approving it writes the system's next revision. Its base_version is the system's revision, 0
for a new system.

Going back deletes the proposals of that kind and their Inbox notices, and puts the check of 0160 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0223"
down_revision = "0221"
branch_labels = None
depends_on = None

KINDS_0160 = (
    "branding",
    "features",
    "plugin_config",
    "agent",
    "policy",
    "problem",
    "goal",
    "opportunity",
    "plugin",
    "plugin_install",
    "plugin_listing",
)
SYSTEM = "system"
KINDS = (*KINDS_0160, SYSTEM)  # tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000
MAX_OUTCOME_CHARS = 2000
PROPOSAL_CHECK = "builder_proposals_check"
NOTICE = "builder_proposal"

col = sa.column


def _proposal_check(kinds) -> None:
    """The check of builder_proposals as 0160 wrote it, its kind one of ``kinds``."""
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
            sa.func.char_length(col("outcome")).between(1, MAX_OUTCOME_CHARS),
        ),
    )


def upgrade() -> None:
    _proposal_check(KINDS)


def downgrade() -> None:
    proposals = sa.table("builder_proposals", col("kind"))
    notifications = sa.table("notifications", col("notice_kind"), col("details", postgresql.JSONB))
    op.execute(
        notifications.delete().where(
            notifications.c.notice_kind == NOTICE, notifications.c.details["kind"].astext == SYSTEM
        )
    )
    op.execute(proposals.delete().where(proposals.c.kind == SYSTEM))
    _proposal_check(KINDS_0160)
