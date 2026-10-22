"""Run autonomy: the principal of a run and the snapshot of its effective autonomy.

Revision ID: 0106
Revises: 0105
Create Date: 2026-10-22

runs gains two columns (plan autonomy-levels, criterion a4): principal_id, the person the run acts for, and autonomy,
the snapshot of that person's effective autonomy the hub took when it queued the run: the level, each class K1 to K12
with its mode, source, detail and condition, the environments and the workers allowed (null: no limit), when it was
taken. A run queued before this revision, and a local run, has neither: it is claimed and gated as before.

Every statement is an Alembic operation on SQLAlchemy Core expressions. Going back drops both columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("principal_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("autonomy", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_foreign_key("runs_principal_id_fkey", "runs", "users", ["principal_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    op.drop_constraint("runs_principal_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "autonomy")
    op.drop_column("runs", "principal_id")
