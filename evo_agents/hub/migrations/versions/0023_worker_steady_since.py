"""When a worker's heartbeats became steady: the next attempt of a run it lost waits for it.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-10

workers.steady_since is the first heartbeat of the worker's current run of heartbeats, none of which came more than
``evo_agents.hub.runs.STEADY_GAP_SECONDS`` after the one before: each heartbeat keeps it, and one that comes later
than that, or the first one, sets it to now. The claim gives the next attempt of a run lost on a worker back to that
worker only once its heartbeats have been steady for ``runs.STEADY_SECONDS`` (``evo_agents.hub.server.runs``), so a
laptop that slept, or woke for a moment, does not take the run again and lose it the same way. One column of the
worker, rather than a row per heartbeat. NULL until the worker's first heartbeat after this revision, which counts as
the start of a run of them. The statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables`` describes the table.

Going back drops the column, which is the schema that schema 0022 runs on: its claims give a next attempt to any
worker that may take it at once.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("steady_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "steady_since")
