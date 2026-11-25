"""Channels a hub admin links: notification_channels.linked_by.

Revision ID: 0268
Revises: 0267
Create Date: 2026-11-25

A member links a channel from a web session of theirs, and it speaks for them while that session lives (0016). A hub
admin may now link a member's channel instead, for a member who does not reach the hub's web: the row keeps no
session (token_id NULL) and names the admin in linked_by, and it lives until it is unlinked or linked again. A row
holds one or the other, never both. An admin who leaves the hub sets linked_by to NULL, and the channel then speaks for
nobody, as a channel whose session ended.

Going back drops the column: a channel an admin linked then has neither, and speaks for nobody until its member links
it from the web.
"""

import sqlalchemy as sa
from alembic import op

revision = "0268"
down_revision = "0267"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_channels", sa.Column("linked_by", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "notification_channels_linked_by_fkey",
        "notification_channels",
        "users",
        ["linked_by"],
        ["id"],
        ondelete="SET NULL",
    )
    rule = sa.or_(sa.column("linked_by").is_(None), sa.column("token_id").is_(None))
    op.create_check_constraint("notification_channels_linked_by_check", "notification_channels", rule)


def downgrade() -> None:
    op.drop_constraint("notification_channels_linked_by_check", "notification_channels", type_="check")
    op.drop_constraint("notification_channels_linked_by_fkey", "notification_channels", type_="foreignkey")
    op.drop_column("notification_channels", "linked_by")
