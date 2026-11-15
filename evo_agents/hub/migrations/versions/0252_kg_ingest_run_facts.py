"""What each pushed run's log says of its source, kept with the run: kg_ingests and kg_pending_runs gain status,
lineage, epoch, run_started_at, run_finished_at and cursor.

Revision ID: 0252
Revises: 0251
Create Date: 2026-11-16

Plan kg-centralized-on-hub, step 3: ``kg sync`` reads the state of a source from the hub instead of a corpus.sqlite
(``evo_agents.hub.kg_source_state``). The hub reads these facts from a run's log when it is pushed
(``evo_agents.hub.kg_ingest.RunFacts``). The logs of the runs the hub holds already are in the blob store, which a
migration does not read: their columns stay null, and the next build of their project, which has every log at hand,
fills them in (``evo_agents.hub.kg_source_state.fill_run_facts``). Going back drops the columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0252"
down_revision = "0251"
branch_labels = None
depends_on = None

TABLES = ("kg_ingests", "kg_pending_runs")
TEXT_COLUMNS = ("status", "lineage", "epoch", "run_started_at", "run_finished_at")


def upgrade() -> None:
    for table in TABLES:
        for name in TEXT_COLUMNS:
            op.add_column(table, sa.Column(name, sa.Text(), nullable=True))
        op.add_column(table, sa.Column("cursor", JSONB(), nullable=True))
    op.create_index("kg_ingests_source_idx", "kg_ingests", ["project_id", "source"])


def downgrade() -> None:
    op.drop_index("kg_ingests_source_idx", table_name="kg_ingests")
    for table in TABLES:
        for name in (*TEXT_COLUMNS, "cursor"):
            op.drop_column(table, name)
