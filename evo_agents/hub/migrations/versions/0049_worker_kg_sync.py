"""What a worker on duty for a night shift last did to keep the project's knowledge graph fresh.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-19

workers.kg_sync is what the worker's last heartbeat said of the projects whose night shift names it: for each, when its
daemon last looked, what stopped it, and for each git source the commit read, how far it was behind, when it was read
and pushed, and the error of the last attempt (``evo_agents.hub.server.runs.models.KgSyncReport``), an object; NULL when
that heartbeat said nothing of it, as an older daemon or one whose sync is off does. GET /v1/workers/{id} shows it and
the night's figures of the project name it (``evo_agents.hub.server.collect``). Every statement is an Alembic operation
on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the table.

Going back drops the column, which is the schema that schema 0048 runs on: its heartbeat neither takes nor keeps what
a worker did for a project's graph.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

CHECK = "workers_kg_sync_check"


def upgrade() -> None:
    op.add_column("workers", sa.Column("kg_sync", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(CHECK, "workers", sa.func.jsonb_typeof(sa.column("kg_sync")) == "object")


def downgrade() -> None:
    op.drop_constraint(CHECK, "workers", type_="check")
    op.drop_column("workers", "kg_sync")
