"""The model a run ran on, and what the routing by difficulty did (plan super-board-parity-2, criterion a6):
runs.model_used and runs.routing.

Revision ID: 0185
Revises: 0184
Create Date: 2026-10-30

runs gains ``model_used``, the model its runtime ran it on as its worker reported it (the runtime's own name, else the
model the worker asked for), and ``routing``, what the worker's rating of its difficulty did
(``evo_agents.hub.model_router.record``: the difficulty, the scorer, what the rating cost and the model it chose). Both
are NULL for a run of 0184 and for a run whose worker said nothing of them.

Going back drops both columns: the hub of 0184 knows only the model a dispatch asked for.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0185"
down_revision = "0184"
branch_labels = None
depends_on = None

TABLE = "runs"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("model_used", sa.Text))
    op.add_column(TABLE, sa.Column("routing", postgresql.JSONB))


def downgrade() -> None:
    op.drop_column(TABLE, "routing")
    op.drop_column(TABLE, "model_used")
