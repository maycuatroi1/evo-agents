"""The proofs a run of one step gives the acceptance criteria its step names.

Revision ID: 0186
Revises: 0185
Create Date: 2026-10-30

runs.proofs holds what the worker of a run of one step reported with done or review: one proof for each criterion of
the plan's acceptance that the step names, ``{"id", "kind", "locator"}`` or ``{"id", "kind": "n/a", "reason"}``
(``evo_agents.hub.proofs``). The step takes them once the run is done, as it takes the run's evidence; NULL for any
other run, and for a run whose step names no criterion.

Going back drops the column; the steps keep the proofs they took.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0186"
down_revision = "0185"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("proofs", postgresql.JSONB(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "proofs")
