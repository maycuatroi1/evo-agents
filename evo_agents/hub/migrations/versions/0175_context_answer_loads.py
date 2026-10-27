"""The confirmed answers a run's prompt carried.

Revision ID: 0175
Revises: 0174
Create Date: 2026-10-28

context_answer_loads is new (plan active-context-answers, criterion a3): one row per answer loaded into a run's prompt,
with the run, its project, the source (decision or memory), the source's id, the scope key that matched it and when. A
run loads an answer once, so a unique key on run, source and source id keeps a run claimed again from adding rows. It
starts empty. The hub counts the rows a day and a week.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0175"
down_revision = "0174"
branch_labels = None
depends_on = None

TABLE = "context_answer_loads"


def _key(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="CASCADE", name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("run_id", "runs"),
        _key("project_id", "projects"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("run_id", "source", "source_id", name=f"{TABLE}_key"),
        sa.CheckConstraint(sa.column("source").in_(("decision", "memory")), name=f"{TABLE}_source_check"),
    )
    op.create_index(f"{TABLE}_project_idx", TABLE, ["project_id", "loaded_at"])


def downgrade() -> None:
    op.drop_table(TABLE)
