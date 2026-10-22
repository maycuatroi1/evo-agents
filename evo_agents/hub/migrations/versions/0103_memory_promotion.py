"""The Curator promotes an agent's memories into the project's shared ones.

Revision ID: 0103
Revises: 0102
Create Date: 2026-10-22

Plan agent-profile-workspace, criterion a4 (``evo_agents.hub.server.memory_promotion`` describes the design). A
proposal of kind memory may carry a promotion, which accepting it writes: proposals.promotion holds it (NULL for every
proposal there is, and for any other). The revision of a shared memory it writes names the proposal
(memory_revisions.proposal_id, a plain id kept as history, like run_id). An agent's memory it promoted takes
memories.promoted_at, when, and memories.promoted_into, the shared memory it went into (NULL once that one is deleted);
a promoted memory leaves the index and the searches of runs. The ledger of a proposal takes a sixteenth action,
'promoted': the hub wrote the shared memory.

The names below are copies of ``evo_agents.hub.tables_memory_agents`` and ``ledger.ACTIONS`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation.

Going back deletes the ledger lines 'promoted', puts back the check of 0058 and drops the four columns; the shared
memories a promotion wrote stay, as any memory does.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

ACTIONS_0102 = (
    "proposed",
    "dropped",
    "accepted",
    "rejected",
    "deferred",
    "planned",
    "built",
    "build_failed",
    "pull_opened",
    "judged",
    "merged",
    "left_open",
    "closed",
    "outcome",
    "skill_release",
)
NEW_ACTIONS = ("promoted",)
ACTIONS = (*ACTIONS_0102, *NEW_ACTIONS)  # evo_agents.hub.ledger.ACTIONS
CHECK = "curator_ledger_action_check"

col = sa.column


def _actions(actions) -> None:
    op.create_check_constraint(CHECK, "curator_ledger", col("action").in_(actions))


def upgrade() -> None:
    op.add_column("proposals", sa.Column("promotion", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("memory_revisions", sa.Column("proposal_id", sa.BigInteger(), nullable=True))
    op.add_column("memories", sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("promoted_into", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "memories_promoted_into_fkey", "memories", "memories", ["promoted_into"], ["id"], ondelete="SET NULL"
    )
    op.drop_constraint(CHECK, "curator_ledger", type_="check")
    _actions(ACTIONS)


def downgrade() -> None:
    ledger = sa.table("curator_ledger", col("action"))
    op.execute(ledger.delete().where(ledger.c.action.in_(NEW_ACTIONS)))
    op.drop_constraint(CHECK, "curator_ledger", type_="check")
    _actions(ACTIONS_0102)
    op.drop_constraint("memories_promoted_into_fkey", "memories", type_="foreignkey")
    op.drop_column("memories", "promoted_into")
    op.drop_column("memories", "promoted_at")
    op.drop_column("memory_revisions", "proposal_id")
    op.drop_column("proposals", "promotion")
