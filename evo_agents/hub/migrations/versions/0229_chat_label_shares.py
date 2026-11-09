"""A writer on a project shares its part of a chat's label with the people of the chat: chat_label_shares.

Revision ID: 0229
Revises: 0228
Create Date: 2026-11-09

A writer on project P who adds an agent of P to a group chat, or adds a person to a chat with an agent of P, shares
P's part of the chat's label with the people there who hold no grant that clears it (``chat_access``): one row per
chat, project and person, with who shared it. A person cleared that way reads what the chat holds of P while they stay
in it and the writer who shared it still holds the writer role and clears the part.

Going back drops the table: the people it cleared no longer clear the chat's label, and the hub refuses them (403).
"""

import sqlalchemy as sa
from alembic import op

revision = "0229"
down_revision = "0228"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_label_shares",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("shared_by", sa.BigInteger(), nullable=False),
        sa.Column("shared_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE", name="chat_label_shares_chat_id_fkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="chat_label_shares_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="chat_label_shares_user_id_fkey"),
        sa.ForeignKeyConstraint(
            ["shared_by"], ["users.id"], ondelete="CASCADE", name="chat_label_shares_shared_by_fkey"
        ),
        sa.PrimaryKeyConstraint("chat_id", "project_id", "user_id", name="chat_label_shares_pkey"),
    )


def downgrade() -> None:
    op.drop_table("chat_label_shares")
