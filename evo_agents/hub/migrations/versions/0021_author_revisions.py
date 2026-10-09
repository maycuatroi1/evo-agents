"""The revisions an author run writes: each names the run.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-09

An author run puts the plan it wrote on the hub (PUT /v1/worker/runs/{id}/plan) as the member who dispatched it, and
``plan_revisions.run_id`` records which run wrote each revision, so the history of a plan says it. NULL for every other
write. A run deleted (going back from 0020 deletes the author runs) leaves its revisions, without the run. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the table.

Going back drops the column, which is the schema that schema 0020 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_revisions", sa.Column("run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "plan_revisions_run_id_fkey", "plan_revisions", "runs", ["run_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("plan_revisions_run_id_fkey", "plan_revisions", type_="foreignkey")
    op.drop_column("plan_revisions", "run_id")
