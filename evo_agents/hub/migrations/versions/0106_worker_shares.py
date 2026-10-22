"""Shared workers narrowed to units of the org chart: worker_shares.

Revision ID: 0106
Revises: 0105
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_org`` as of this revision, never imports, so a later change to
it takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

worker_shares holds the units of the org chart a shared worker is narrowed to (plan credential-sharing, criterion
a5): the worker, the unit, who added the row and when. A shared worker with no row takes the runs of every member
holding writer on a project it serves, as before; one with rows takes, besides its owner's, only those of a member
whose live primary seat is in one of those units or below one (``evo_agents.hub.server.runs.service.worker_fit``).
Who may write is the service's (``evo_agents.hub.server.worker_shares``).

Going back turns ``shared`` off on every worker that has a row, so no worker narrowed to a unit is shared with every
writer once the rows are gone, then drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

SHARES = "worker_shares"

col = sa.column


def _key(column: str, target: str, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{SHARES}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        SHARES,
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("worker_id", "workers", "CASCADE"),
        _key("unit_id", "org_units", "RESTRICT"),
        _key("created_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("worker_id", "unit_id", name=f"{SHARES}_pkey"),
    )
    op.create_index(f"{SHARES}_unit_idx", SHARES, ["unit_id"])


def downgrade() -> None:
    workers, shares = sa.table("workers", col("id"), col("shared")), sa.table(SHARES, col("worker_id"))
    narrowed = sa.exists().where(shares.c.worker_id == workers.c.id)
    op.execute(workers.update().values(shared=False).where(narrowed))
    op.drop_table(SHARES)
