"""A Telegram chat lives as long as the web session that linked it, and a brief outlives its notification.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08

``evo_agents.hub.server.telegram`` describes the design. Every statement is an Alembic operation on SQLAlchemy Core
expressions, as ``evo_agents.hub.tables`` describes the tables.

notification_channels.token_id names the web session that linked the channel, and telegram_links.token_id the one that
made the code: only a web session links a Telegram chat, and the chat is answered and sent to while that session
lives. Deleting the session's row deletes both. A channel or code made before this revision names no session, so it is
treated as one whose session ended: its member links the chat again.

curator_briefs.notification_id loses its value when its notification is deleted (ON DELETE SET NULL) instead of
refusing the delete; the brief stays.

Going back drops the two columns, their keys and the index, and puts the foreign key of 0015 back, which is the schema
that schema 0015 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

BRIEF_FKEY = "curator_briefs_notification_id_fkey"


def upgrade() -> None:
    op.add_column("notification_channels", sa.Column("token_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "notification_channels_token_id_fkey",
        "notification_channels",
        "tokens",
        ["token_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("notification_channels_token_idx", "notification_channels", ["token_id"])

    op.add_column("telegram_links", sa.Column("token_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "telegram_links_token_id_fkey", "telegram_links", "tokens", ["token_id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint(BRIEF_FKEY, "curator_briefs", type_="foreignkey")
    op.create_foreign_key(
        BRIEF_FKEY, "curator_briefs", "notifications", ["notification_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(BRIEF_FKEY, "curator_briefs", type_="foreignkey")
    op.create_foreign_key(BRIEF_FKEY, "curator_briefs", "notifications", ["notification_id"], ["id"])

    op.drop_constraint("telegram_links_token_id_fkey", "telegram_links", type_="foreignkey")
    op.drop_column("telegram_links", "token_id")

    op.drop_index("notification_channels_token_idx", table_name="notification_channels")
    op.drop_constraint("notification_channels_token_id_fkey", "notification_channels", type_="foreignkey")
    op.drop_column("notification_channels", "token_id")
