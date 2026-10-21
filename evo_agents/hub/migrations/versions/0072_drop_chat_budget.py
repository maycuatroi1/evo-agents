"""No chat budget: the runs of a chat's agents no longer stop at a cost the chat sets.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-21

chats loses budget_usd, what its chat runs could cost in 24 hours, and chats_budget_check with it. A chat, task or
triage run still stops at its time cap (``evo_agents.hub.chat_runs.TIME_CAP_SECONDS``).

The values below are copies of revision 0032 as of this revision, never imports. Every statement is an Alembic
operation on SQLAlchemy Core expressions.

Going back puts budget_usd back on every chat at 5, its default, with chats_budget_check: more than 0, at most 1000.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

DEFAULT_BUDGET_USD = 5  # 0032
MAX_BUDGET_USD = 1000  # 0032

col = sa.column


def upgrade() -> None:
    op.drop_constraint("chats_budget_check", "chats", type_="check")
    op.drop_column("chats", "budget_usd")


def downgrade() -> None:
    op.add_column("chats", sa.Column("budget_usd", sa.Double(), server_default=str(DEFAULT_BUDGET_USD), nullable=False))
    op.create_check_constraint(
        "chats_budget_check", "chats", sa.and_(col("budget_usd") > 0, col("budget_usd") <= MAX_BUDGET_USD)
    )
