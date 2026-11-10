"""The thread of a direct chat a group chat was opened from: chats.opened_from_message_id.

Revision ID: 0232
Revises: 0231
Create Date: 2026-11-10

A mention in a direct chat of an agent it does not have, by its person or by its agent (``evo-agents worker chat
mention``), opens a group chat of that person, the direct chat's agent and the agent mentioned
(``evo_agents.hub.server.chat_groups``). The group keeps the first message of the direct chat's thread it came from,
so that a later mention in that thread adds to the same group instead of opening another: one group per thread, held by
the partial unique index chats_opened_from_key. NULL: a chat made otherwise, as every chat before. The column allows
NULL and has no default, so the code before it runs on the schema after it. The message going (its chat deleted) sets it
NULL.

Going back drops the column and its index: the groups stay, and a later mention opens another.
"""

import sqlalchemy as sa
from alembic import op

revision = "0232"
down_revision = "0231"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("opened_from_message_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "chats_opened_from_message_id_fkey",
        "chats",
        "chat_messages",
        ["opened_from_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "chats_opened_from_key",
        "chats",
        ["opened_from_message_id"],
        unique=True,
        postgresql_where=sa.text("opened_from_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("chats_opened_from_key", table_name="chats")
    op.drop_constraint("chats_opened_from_message_id_fkey", "chats", type_="foreignkey")
    op.drop_column("chats", "opened_from_message_id")
