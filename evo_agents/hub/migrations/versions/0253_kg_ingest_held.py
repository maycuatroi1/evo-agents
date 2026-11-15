"""The removals a pushed run's deletion guard held back, kept with the run: kg_ingests and kg_pending_runs gain held.

Revision ID: 0253
Revises: 0252
Create Date: 2026-11-16

Plan kg-centralized-on-hub, step 3: ``kg sync --accept-removals`` reads the removals a guard held back from the hub
instead of the held table of a corpus.sqlite (``evo_agents.hub.kg_source_state``). The hub reads them from the run_end
of a run's log when it is pushed (``evo_agents.hub.kg_ingest.RunFacts``). A run pushed before held nothing the hub
knows of: its column stays null, as the next build leaves it (a log of then lists no held removals). Going back drops
the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0253"
down_revision = "0252"
branch_labels = None
depends_on = None

TABLES = ("kg_ingests", "kg_pending_runs")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("held", JSONB(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "held")
