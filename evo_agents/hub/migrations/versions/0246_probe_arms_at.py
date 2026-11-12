"""Probe runs: where the arms of a week's probe ran.

Revision ID: 0246
Revises: 0245
Create Date: 2026-11-12

Plan probes-measure-both-arms, criterion a2 (``evo_agents.hub.server.probe_runs``). curator_probe_runs gain
``arms_at``: ``tip`` when arm on ran at the tip of the default branch of the change's repo and arm off at that tip less
the change's diff, ``change`` when that diff no longer came off the tip and both arms ran at the change's own commits
(its merge commit and its first parent, or its head and merge base). NULL while the row has no session, and for a
plugin's probe. A row of a change's probe with sessions before this revision ran at the tip, the only way workers had:
it is set to ``tip``.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0246"
down_revision = "0245"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("curator_probe_runs", sa.Column("arms_at", sa.Text()))
    op.create_check_constraint(
        "curator_probe_runs_arms_at_check", "curator_probe_runs", sa.column("arms_at").in_(("tip", "change"))
    )
    op.execute(
        "UPDATE curator_probe_runs SET arms_at = 'tip' WHERE change_id IS NOT NULL AND jsonb_array_length(sessions) > 0"
    )


def downgrade() -> None:
    op.drop_column("curator_probe_runs", "arms_at")
