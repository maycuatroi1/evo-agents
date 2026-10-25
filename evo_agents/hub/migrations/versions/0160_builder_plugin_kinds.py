"""@builder's proposals of a new plugin, of installing it on the hub, and of listing it on a market.

Revision ID: 0160
Revises: 0159
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_settings`` and ``evo_agents.core_plugins.builder.proposals`` as
of this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic
operation on SQLAlchemy Core expressions.

builder_proposals.kind takes three kinds of an action (plan plugin-market, criterion a8): ``plugin``, a plugin a member
asked @builder to make for an industry; ``plugin_install``, installing the release of its repo on the hub;
``plugin_listing``, listing it on a market. Approving one does the action once and rolls nothing back: its
applied_version is 1. Two columns say what it did: opened_run_id, the chat run of @builder approving opened (NULL once
that run is deleted), and outcome, 1 to MAX_OUTCOME_CHARS characters or NULL.

Going back deletes the proposals of the three kinds and their Inbox notices, drops the two columns and puts the check
of 0143 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None

KINDS_0143 = ("branding", "features", "plugin_config", "agent", "policy", "problem", "goal", "opportunity")
ACTIONS = ("plugin", "plugin_install", "plugin_listing")
KINDS = (*KINDS_0143, *ACTIONS)  # tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000
MAX_OUTCOME_CHARS = 2000
PROPOSAL_CHECK = "builder_proposals_check"
NOTICE = "builder_proposal"

col = sa.column


def _proposal_check(kinds, *outcome) -> None:
    """The check of builder_proposals as 0043 wrote it, its kind one of ``kinds``, with ``outcome``'s terms."""
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
            *outcome,
        ),
    )


def upgrade() -> None:
    op.add_column("builder_proposals", sa.Column("opened_run_id", sa.BigInteger()))
    op.add_column("builder_proposals", sa.Column("outcome", sa.Text()))
    op.create_foreign_key(
        "builder_proposals_opened_run_id_fkey",
        "builder_proposals",
        "runs",
        ["opened_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _proposal_check(KINDS, sa.func.char_length(col("outcome")).between(1, MAX_OUTCOME_CHARS))


def downgrade() -> None:
    proposals = sa.table("builder_proposals", col("kind"))
    notifications = sa.table("notifications", col("notice_kind"), col("details", postgresql.JSONB))
    op.execute(
        notifications.delete().where(
            notifications.c.notice_kind == NOTICE, notifications.c.details["kind"].astext.in_(ACTIONS)
        )
    )
    op.execute(proposals.delete().where(proposals.c.kind.in_(ACTIONS)))
    _proposal_check(KINDS_0143)
    op.drop_constraint("builder_proposals_opened_run_id_fkey", "builder_proposals", type_="foreignkey")
    op.drop_column("builder_proposals", "outcome")
    op.drop_column("builder_proposals", "opened_run_id")
