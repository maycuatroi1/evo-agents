"""The outcome of a trace's work, the person who marked it handled, and the hub's lines that repeat a message.

Revision ID: 0270
Revises: 0269
Create Date: 2026-11-26

system_traces takes outcome, what the system's entry agent recorded of its work against its done-when with
``evo-agents worker work set --outcome`` (``evo_agents.hub.server.work_cards``): met or not_met, NULL when it recorded
none; outcome_left, what it named as still to do (``--left``), NULL for nothing; handled_by and handled_at, the member
of the work chat who marked the work handled and when (POST /v1/chats/{id}/work/handled), NULL until one does, and the
person set to NULL when they are deleted. chat_messages takes repeats_id, the message of the same work chat that a line
of the hub repeats ("asked ... (run #N)", "answered (run #N)"; ``work_chats.say``), set to NULL when that message is
deleted. Every column allows NULL and has no default, nothing is backfilled, and the code before it runs on the schema
after it.

Going back drops the five columns, with what they recorded.
"""

import sqlalchemy as sa
from alembic import op

revision = "0270"
down_revision = "0269"
branch_labels = None
depends_on = None

OUTCOMES = ("met", "not_met")  # evo_agents.hub.server.work_status.OUTCOMES, a copy
OUTCOME_CHECK = "system_traces_outcome_check"


def upgrade() -> None:
    op.add_column("system_traces", sa.Column("outcome", sa.Text(), nullable=True))
    op.create_check_constraint(OUTCOME_CHECK, "system_traces", sa.column("outcome").in_(OUTCOMES))
    op.add_column("system_traces", sa.Column("outcome_left", sa.Text(), nullable=True))
    op.add_column("system_traces", sa.Column("handled_by", sa.BigInteger(), nullable=True))
    op.add_column("system_traces", sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "system_traces_handled_by_fkey", "system_traces", "users", ["handled_by"], ["id"], ondelete="SET NULL"
    )
    op.add_column("chat_messages", sa.Column("repeats_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "chat_messages_repeats_id_fkey", "chat_messages", "chat_messages", ["repeats_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("chat_messages_repeats_id_fkey", "chat_messages", type_="foreignkey")
    op.drop_column("chat_messages", "repeats_id")
    op.drop_constraint("system_traces_handled_by_fkey", "system_traces", type_="foreignkey")
    op.drop_constraint(OUTCOME_CHECK, "system_traces", type_="check")
    for column in ("handled_at", "handled_by", "outcome_left", "outcome"):
        op.drop_column("system_traces", column)
