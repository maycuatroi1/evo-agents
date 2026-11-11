"""Worker health: the free disk of a worker's worktrees and the login of each of its runtimes.

Revision ID: 0239
Revises: 0238
Create Date: 2026-11-11

workers takes health, what the worker's last heartbeat said of its machine (plan hub-steward, criterion a3):
{"disk": {"free_gb", "free_pct"}, "logins": {<runtime>: ok | unauthorized | missing | unknown}}. NULL: its heartbeat
said nothing of it, as an older daemon's. The column allows NULL and has no default, so the code before it runs on the
schema after it.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0239"
down_revision = "0238"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("health", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "health")
