"""The Curator's changes of a harness whose replays won: the ledger's line of the global skill the hub publishes.

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-20

``evo_agents.hub.tiers.replay_tier``, ``evo_agents.hub.replay.verdict`` and ``evo_agents.hub.server.skill_release``
describe the design; the list below is a copy of ``ledger.ACTIONS`` as of this revision, never an import, so a later
change to it takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

curator_ledger.action takes a fifteenth value, 'skill_release': the hub published a version of a global skill from the
merge commit of a change of the Curator whose replays won, or published the version before it again after the change's
figures got worse, or says why it published none. A merge's verdict of its replays and the skill it releases live in
the details of its line 'merged', which need no column.

Going back deletes the lines 'skill_release' and puts back the check of 0018, which is the schema that schema 0054 runs
on; the versions of the skills the hub published stay, as any version does.
"""

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

ACTIONS_0054 = (
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
)
NEW_ACTIONS = ("skill_release",)
ACTIONS = (*ACTIONS_0054, *NEW_ACTIONS)  # evo_agents.hub.ledger.ACTIONS

col = sa.column


def _actions(actions) -> None:
    op.create_check_constraint("curator_ledger_action_check", "curator_ledger", col("action").in_(actions))


def upgrade() -> None:
    op.drop_constraint("curator_ledger_action_check", "curator_ledger", type_="check")
    _actions(ACTIONS)


def downgrade() -> None:
    ledger = sa.table("curator_ledger", col("action"))
    op.execute(ledger.delete().where(ledger.c.action.in_(NEW_ACTIONS)))
    op.drop_constraint("curator_ledger_action_check", "curator_ledger", type_="check")
    _actions(ACTIONS_0054)
