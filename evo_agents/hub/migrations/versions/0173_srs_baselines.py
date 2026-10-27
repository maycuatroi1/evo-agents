"""A goal that aims at an SRS document has its baseline confirmed once by an admin.

Revision ID: 0173
Revises: 0172
Create Date: 2026-10-28

srs_baselines is new (plan srs-backlog-loop, criterion a5): one row per goal whose baseline of delivered, undelivered and
unknown use cases an admin confirmed, the document it is of, who confirmed and when, and the count of each state then.
The Loop makes no build for a goal without a row for the document it aims at. It starts empty: no goal is confirmed.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0173"
down_revision = "0172"
branch_labels = None
depends_on = None

TABLE = "srs_baselines"


def _key(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("goal_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_by", sa.BigInteger()),
        sa.Column("counts", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("goal_id", "goals", "CASCADE"),
        _key("project_id", "projects", "CASCADE"),
        _key("document_id", "srs_documents", "CASCADE"),
        _key("confirmed_by", "users", "SET NULL"),
        sa.PrimaryKeyConstraint("goal_id", name="srs_baselines_pkey"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
