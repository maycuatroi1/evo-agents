"""The channel a decision was answered through: decisions.answered_via.

Revision ID: 0259
Revises: 0258
Create Date: 2026-11-19

A member answers a decision on the web, with the CLI, or through a channel of a core plugin (``core_plugin_api``
``answer_decision``). decisions gains answered_via, the kind of channel the answer came through, such as telegram:
NULL for an answer from the web or the CLI, for a decision still open and for one the hub answered itself, and for
every decision answered before this revision. It is 1 to 64 characters. Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0259"
down_revision = "0258"
branch_labels = None
depends_on = None

MAX_VIA_CHARS = 64  # evo_agents.hub.tables_decisions, a copy


def upgrade() -> None:
    op.add_column("decisions", sa.Column("answered_via", sa.Text(), nullable=True))
    via = sa.func.char_length(sa.column("answered_via"))
    op.create_check_constraint("decisions_answered_via_check", "decisions", via.between(1, MAX_VIA_CHARS))


def downgrade() -> None:
    op.drop_column("decisions", "answered_via")
