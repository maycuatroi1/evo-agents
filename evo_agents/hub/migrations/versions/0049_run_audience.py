"""What the agent of each run was shown, as an audience, so the memories it writes carry it.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-19

runs.seen_audience holds the principals (``chat:<id>``, ``grant:<project>:<level>``, as ``evo_agents.kg.policy`` names
them) of what the hub returned to the agent of the run with an audience: the labels of kg_* results, the messages
chat_message, chat_thread and `evo-agents worker chat read` read, and the memories it read
(``evo_agents.hub.server.run_audience``). It only grows, sorted and without repeats; empty, the default for every run
there is and every new one. ``evo_agents.hub.tables_chats`` describes the column. Every statement is an Alembic
operation on SQLAlchemy Core expressions.

Going back drops the column: a memory written meanwhile keeps the audience its label took.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    column = sa.Column("seen_audience", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False)
    op.add_column("runs", column)


def downgrade() -> None:
    op.drop_column("runs", "seen_audience")
