"""A worker its owner shares with the writers of the projects it serves.

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-20

workers.shared is what its owner sets with PATCH /v1/workers/{id}: true, the worker also takes the runs of every other
member who holds writer on a project it serves (``docs/workers.md``, Shared workers); false, the default for every
worker there is and every new one, it takes only its owner's runs, as before. Every statement is an Alembic operation
on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the table.

Going back drops the column, which is the schema that schema 0061 runs on: no worker is shared there. The runs of other
members a shared worker held or had queued stay as they are; a worker of schema 0061 claims none of them, and their
dispatchers cancel or dispatch them again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("workers", "shared")
