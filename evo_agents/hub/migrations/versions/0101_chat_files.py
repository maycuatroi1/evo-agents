"""The files people attach to chat messages: chat_files.

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_chats`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

chat_files holds each file a member of a chat uploaded to attach to a message: its chat, the member who uploaded it,
its name, media type, size and SHA-256, its bytes a blob of the blob store that the row refers to (``blob_gc``). It is
uploaded first, with no message, and the message that names it then carries it (``message_id``); deleting the chat or
the message deletes it.

A message that carries files may have no text, so a chat message's body may now be empty (0 to 16384 bytes of UTF-8,
1 to 16384 before).

Going back drops the table, giving each empty body a placeholder first; the files' blobs are left for the store's own
cleanup.
"""

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

TABLE = "chat_files"
BODY_CHECK = "chat_messages_body_check"
MAX_MESSAGE_BYTES = 16 * 1024  # evo_agents.hub.chats.MAX_MESSAGE_BYTES
NO_TEXT = "(files)"  # the body an empty one takes going back


def _body_check(least: int) -> None:
    op.drop_constraint(BODY_CHECK, "chat_messages", type_="check")
    shape = sa.func.octet_length(sa.column("body")).between(least, MAX_MESSAGE_BYTES)
    op.create_check_constraint(BODY_CHECK, "chat_messages", shape)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE", name=f"{TABLE}_chat_id_fkey"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"], ondelete="CASCADE", name=f"{TABLE}_message_id_fkey"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL", name=f"{TABLE}_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
    )
    op.create_index(
        "chat_files_message_idx",
        TABLE,
        ["message_id"],
        postgresql_where=sa.column("message_id").is_not(None),
    )
    op.create_index("chat_files_sha256_idx", TABLE, ["sha256"])
    _body_check(0)


def downgrade() -> None:
    messages = sa.table("chat_messages", sa.column("body", sa.Text()))
    op.execute(sa.update(messages).where(messages.c.body == "").values(body=NO_TEXT))
    _body_check(1)
    op.drop_table(TABLE)
