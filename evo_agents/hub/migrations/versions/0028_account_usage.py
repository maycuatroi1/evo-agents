"""Account usage: what the last rate_limit_event a run reported said of an account, and when.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-13

``evo_agents.hub.server.rate_limits`` writes both: a worker reports the usage its agent's CLI gave for the account a
run holds, and the hub keeps the last one. last_usage is a JSON object (status, rate_limit_type, resets_at,
utilization, windows), never a credential value; last_usage_at is when the hub stored it. Both are null for an
account no run reported on, which is every account after this revision: nothing that exists changes meaning.

Going back drops the two columns, which is the schema that schema 0027 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("last_usage", postgresql.JSONB(), nullable=True))
    op.add_column("accounts", sa.Column("last_usage_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "accounts_last_usage_check",
        "accounts",
        sa.func.jsonb_typeof(sa.column("last_usage")) == "object",
    )


def downgrade() -> None:
    op.drop_constraint("accounts_last_usage_check", "accounts", type_="check")
    op.drop_column("accounts", "last_usage_at")
    op.drop_column("accounts", "last_usage")
