"""Why a run failed, as its worker says it: the cause the Curator's figures count, without guessing from the error.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-10

runs.failure_cause is the cause the worker reported with the state failed (``failure_cause`` of the state report, an
optional field of the worker protocol version 1): ``origin``, ``credentials`` and ``missing_tool`` for a run its
preflight stopped before the agent started, ``push_conflict``, ``verify_failed``, ``timeout`` and the others of
``evo_agents.hub.runs.FAILURE_CAUSES``. NULL for a run that did not fail, one an older daemon ended, one the hub ended
itself, and every run before this revision: for those, the figures guess the cause from the error as before
(``evo_agents.hub.review.run_cause``). One text column, no constraint: the hub checks the name against
``runs.FAILURE_CAUSE`` when it takes the report. The statement is an Alembic operation on SQLAlchemy Core expressions,
as ``evo_agents.hub.tables`` describes the table.

Going back drops the column, which is the schema that schema 0023 runs on: its reports carry no cause, and its figures
guess every cause from the error.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("failure_cause", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "failure_cause")
