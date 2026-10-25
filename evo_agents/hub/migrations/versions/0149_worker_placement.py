"""Taints of workers, and the selector and tolerations of runs.

Revision ID: 0149
Revises: 0148
Create Date: 2026-10-25

A claim places a run by labels and taints (plan worker-pools, criteria a2 and a3). workers.taints holds the taints the
owner set on a worker (PATCH /v1/workers/{id}); a node of a pool also has its pool's. runs.selector holds the labels a
run asks of the worker that claims it, every one of them, and runs.tolerations the taints it tolerates: a worker with
a taint the run does not tolerate leaves the run to others, unless the run is pinned to it by name. Each is empty by
default, so a run dispatched before this revision, with no selector, is claimed by a worker without taints as before.
Only columns are added.

Going back drops the three columns, which is the schema 0148 runs on: its hub places runs by neither.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None

COLUMNS = (("workers", "taints"), ("runs", "selector"), ("runs", "tolerations"))


def upgrade() -> None:
    for table, name in COLUMNS:
        op.add_column(table, sa.Column(name, postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False))


def downgrade() -> None:
    for table, name in reversed(COLUMNS):
        op.drop_column(table, name)
