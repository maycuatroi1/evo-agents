"""A plan run with a new agent session for each step, and what a worker's daemon says it does besides its kinds of run.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-12

runs.session_per_step is true for a plan run dispatched with ``session_per_step``: its worker starts a new agent
session for each step not done yet, whose prompt is the step's with the handoff of the plan, instead of one session
for every step. False for every other run and every run before this revision, which ran in one session; a run that
resumes a parked one keeps it. workers.features lists what the worker's daemon said it does besides its kinds of run
(``features`` of the heartbeat, an optional field of the worker protocol version 1; ``runs.WORKER_FEATURES``):
``session_per_step``. NULL for a worker whose daemon said none, an older one, which never claims a plan run with a
session per step, and the hub refuses to pin one to it. The statements are Alembic operations on SQLAlchemy Core
expressions, as ``evo_agents.hub.tables`` describes the tables.

Going back drops both columns, which is the schema that schema 0024 runs on: its plan runs run in one session, and
its claim does not look at features.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("session_per_step", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("workers", sa.Column("features", postgresql.ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "features")
    op.drop_column("runs", "session_per_step")
