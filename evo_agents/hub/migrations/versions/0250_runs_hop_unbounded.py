"""No hop limit: a chat run may be at any hop of its chain.

Revision ID: 0250
Revises: 0249
Create Date: 2026-11-15

runs_hop_check (0032) took a hop from 1 to 3, the hops a chain of asks and mentions could reach
(``evo_agents.hub.chat_runs.MAX_HOPS``, gone with this revision). It now takes any hop from 1: an agent still never
hands work to itself (``evo_agents.hub.chat_runs.hop_problem``).

The values below are copies of revision 0032 as of this revision, never imports. Every statement is an Alembic
operation on SQLAlchemy Core expressions.

Going back sets every hop past 3 to 3, the deepest hop 0032 takes, and restores the check of 0032.
"""

import sqlalchemy as sa
from alembic import op

revision = "0250"
down_revision = "0249"
branch_labels = None
depends_on = None

MAX_HOPS_0032 = 3
CHECK = "runs_hop_check"

col = sa.column


def upgrade() -> None:
    op.drop_constraint(CHECK, "runs", type_="check")
    op.create_check_constraint(CHECK, "runs", col("hop") >= 1)


def downgrade() -> None:
    runs = sa.table("runs", col("hop"))
    op.execute(sa.update(runs).values(hop=MAX_HOPS_0032).where(runs.c.hop > MAX_HOPS_0032))
    op.drop_constraint(CHECK, "runs", type_="check")
    op.create_check_constraint(CHECK, "runs", col("hop").between(1, MAX_HOPS_0032))
