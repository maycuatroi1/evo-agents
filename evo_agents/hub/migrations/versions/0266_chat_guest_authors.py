"""Guests of a bridged group: chat_messages.guest_name, and the rule of a message's author that takes it.

Revision ID: 0266
Revises: 0265
Create Date: 2026-11-23

chat_messages gains guest_name, the name a core plugin gave the sender of a bridged group's message who linked no
account of the hub (``core_plugin_api.bridge_guest``): 1 to 200 characters, NULL for every message of a person, an
agent or the hub, and for every message before this revision. The rule of a message's author (chat_messages_who_check)
now reads: at most one of author_user_id, author_profile_id and guest_name; none of them is the hub.

Going back keeps each guest's message as a message of the hub, its body led by "<name> (guest): " where that still fits
in a message (MAX_MESSAGE_BYTES), its body alone otherwise, then drops the column and puts back the rule of two.
"""

import sqlalchemy as sa
from alembic import op

revision = "0266"
down_revision = "0265"
branch_labels = None
depends_on = None

MAX_GUEST_CHARS = 200  # evo_agents.hub.chats.MAX_GUEST_CHARS, a copy
MAX_MESSAGE_BYTES = 16 * 1024  # evo_agents.hub.chats.MAX_MESSAGE_BYTES, a copy
WHO = "chat_messages_who_check"

col = sa.column


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("guest_name", sa.Text(), nullable=True))
    op.create_check_constraint(
        "chat_messages_guest_name_check",
        "chat_messages",
        sa.func.char_length(col("guest_name")).between(1, MAX_GUEST_CHARS),
    )
    op.drop_constraint(WHO, "chat_messages", type_="check")
    op.create_check_constraint(
        WHO,
        "chat_messages",
        sa.func.num_nonnulls(col("author_user_id"), col("author_profile_id"), col("guest_name")) <= 1,
    )


def downgrade() -> None:
    messages = sa.table("chat_messages", col("guest_name", sa.Text()), col("body", sa.Text()))
    led = col("guest_name") + " (guest): " + col("body")
    fits = sa.func.octet_length(led) <= MAX_MESSAGE_BYTES
    op.execute(
        sa.update(messages)
        .where(messages.c.guest_name.is_not(None))
        .values(body=sa.case((fits, led), else_=messages.c.body))
    )
    op.drop_constraint(WHO, "chat_messages", type_="check")
    op.drop_column("chat_messages", "guest_name")
    op.create_check_constraint(
        WHO, "chat_messages", sa.or_(col("author_user_id").is_(None), col("author_profile_id").is_(None))
    )
