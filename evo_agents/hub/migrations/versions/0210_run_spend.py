"""What each run spent: run_spend, its tokens and cost by agent session and model, and runs.spend_at.

Revision ID: 0210
Revises: 0209
Create Date: 2026-11-06

A worker reported a run's usage with the report that ended it, so a run that was running, waiting or parked, or that
ended done because a new run resumed it, showed no tokens and no cost, and a run that went on in a session counted
the earlier runs' spend of that session again. run_spend holds what each run spent itself, by session and model, as
``evo_agents.hub.run_spend`` reads its usage reports, and ``spend_store.record`` writes it as they come. This revision
adds the table and the column only: the job hub.record_spend takes in the runs before it (spend_at NULL), oldest
first.

Going back drops both.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None


def _tokens(name: str) -> sa.Column:
    return sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    op.add_column("runs", sa.Column("spend_at", sa.DateTime(timezone=True)))
    op.create_table(
        "run_spend",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        _tokens("input_tokens"),
        _tokens("output_tokens"),
        _tokens("cache_read_tokens"),
        _tokens("cache_write_tokens"),
        _tokens("cache_write_1h_tokens"),
        _tokens("reasoning_tokens"),
        sa.Column("reported_usd", sa.Numeric(14, 6)),
        sa.Column("estimated_usd", sa.Numeric(14, 6)),
        sa.Column("reports", sa.Integer(), nullable=False),
        sa.Column("ends", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_spend_run_id_fkey"),
        sa.PrimaryKeyConstraint("run_id", "session", "model", name="run_spend_pkey"),
    )
    op.execute("CREATE INDEX run_spend_session_idx ON run_spend (session, run_id DESC) WHERE session <> ''")


def downgrade() -> None:
    op.drop_index("run_spend_session_idx", table_name="run_spend")
    op.drop_table("run_spend")
    op.drop_column("runs", "spend_at")
