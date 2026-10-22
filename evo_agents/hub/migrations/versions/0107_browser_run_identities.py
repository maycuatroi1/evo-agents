"""Browser identities of runs: runs.browser_identities.

Revision ID: 0107
Revises: 0106
Create Date: 2026-10-23

A run dispatched with identities (plan agent-browser, criterion a4) names them in runs.browser_identities, the ids of
their browser_identities rows in the order the dispatch named them: its Chromium starts on the profile of the first,
with the cookies of the others. The array is empty for every run before this revision and for one dispatched without
identities. The hub hands such a run only to the worker its identities are on, while no other run holds one of them,
and gives them to it (browser_identities.held_by_run) for as long as a worker holds the run. Every statement is an
Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

Going back drops the column, which is the schema that schema 0106 runs on: a run gets a Chromium with an empty
profile, and no identity is held by one.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("browser_identities", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runs", "browser_identities")
