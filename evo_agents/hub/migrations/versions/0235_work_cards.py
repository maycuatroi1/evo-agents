"""Work cards: what a work chat's trace is for, and the hub's message that shows its card.

Revision ID: 0235
Revises: 0234
Create Date: 2026-11-10

system_traces takes title, goal and done_when, which the system's entry agent sets with ``evo-agents worker work set``
(``evo_agents.hub.server.work_cards``); NULL: not set, and the card then shows the first line of the trace's first
message as its title. card_message_id names the hub's message in the root thread that carries the trace's work card;
NULL: a trace with no work chat yet, or one whose card message went. card_state holds what the card showed when the hub
last told the chat's streams of it, so a move that changes nothing on the card wakes no stream; NULL: never told.
Every column allows NULL and has no default, so the code before it runs on the schema after it. Two partial indexes find
a trace by its card message (each page of messages looks for cards) and by its work chat (the list of chats).

Going back drops the indexes and the five columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0235"
down_revision = "0234"
branch_labels = None
depends_on = None

TEXTS = ("title", "goal", "done_when", "card_state")
INDEXES = (("system_traces_card_message_idx", "card_message_id"), ("system_traces_work_chat_idx", "work_chat_id"))


def upgrade() -> None:
    for column in TEXTS:
        op.add_column("system_traces", sa.Column(column, sa.Text(), nullable=True))
    op.add_column("system_traces", sa.Column("card_message_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "system_traces_card_message_id_fkey",
        "system_traces",
        "chat_messages",
        ["card_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for name, column in INDEXES:
        op.create_index(name, "system_traces", [column], postgresql_where=sa.column(column).is_not(None))


def downgrade() -> None:
    for name, _ in INDEXES:
        op.drop_index(name, table_name="system_traces")
    op.drop_constraint("system_traces_card_message_id_fkey", "system_traces", type_="foreignkey")
    for column in ("card_message_id", *TEXTS):
        op.drop_column("system_traces", column)
