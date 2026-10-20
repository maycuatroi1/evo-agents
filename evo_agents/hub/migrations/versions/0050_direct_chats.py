"""Direct chats: a chat of one person and one agent, which that person opens with the agent's handle.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-20

chats.kind says what a chat is, ``group`` (every chat there is, and the default) or ``direct``; a direct chat names its
person (``direct_user_id``) and its agent (``direct_profile_id``), and a group chat neither (chats_direct_check).
chats_direct_key holds one direct chat per (person, agent): two requests that open the same one at once make one row,
the second finding the first's (``evo_agents.hub.server.chat_direct``). chat_mentions_via_check takes ``direct``, how a
message of a direct chat without an @ of its agent reached it. ``evo_agents.hub.tables_chats`` describes the columns
and the key; the CHECKs are this file's alone. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back makes every direct chat a group chat of the same person and agent, and a mention that reached its agent
``via`` direct reads as ``via`` thread, how a message without an @ reached an agent before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

GROUP, DIRECT = "group", "direct"  # evo_agents.hub.chats.CHAT_KINDS
VIA_0031 = ("login", "handle", "project", "thread")
VIA = (*VIA_0031, DIRECT)  # evo_agents.hub.chats.VIA

col = sa.column


def _via(values) -> None:
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    rule = sa.or_(col("via").is_(None), col("via").in_(values))
    op.create_check_constraint("chat_mentions_via_check", "chat_mentions", rule)


def upgrade() -> None:
    op.add_column("chats", sa.Column("kind", sa.Text(), server_default=GROUP, nullable=False))
    op.add_column("chats", sa.Column("direct_user_id", sa.BigInteger(), nullable=True))
    op.add_column("chats", sa.Column("direct_profile_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint("chats_kind_check", "chats", col("kind").in_((GROUP, DIRECT)))
    pair = sa.or_(
        sa.and_(col("kind") == GROUP, col("direct_user_id").is_(None), col("direct_profile_id").is_(None)),
        sa.and_(col("kind") == DIRECT, col("direct_user_id").is_not(None), col("direct_profile_id").is_not(None)),
    )
    op.create_check_constraint("chats_direct_check", "chats", pair)
    op.create_foreign_key(
        "chats_direct_user_id_fkey", "chats", "users", ["direct_user_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        "chats_direct_profile_id_fkey", "chats", "agent_profiles", ["direct_profile_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_unique_constraint("chats_direct_key", "chats", ["direct_user_id", "direct_profile_id"])
    _via(VIA)


def downgrade() -> None:
    mentions = sa.table("chat_mentions", col("via"))
    op.execute(sa.update(mentions).where(mentions.c.via == DIRECT).values(via="thread"))
    _via(VIA_0031)
    op.drop_constraint("chats_direct_key", "chats", type_="unique")
    op.drop_constraint("chats_direct_profile_id_fkey", "chats", type_="foreignkey")
    op.drop_constraint("chats_direct_user_id_fkey", "chats", type_="foreignkey")
    op.drop_constraint("chats_direct_check", "chats", type_="check")
    op.drop_constraint("chats_kind_check", "chats", type_="check")
    op.drop_column("chats", "direct_profile_id")
    op.drop_column("chats", "direct_user_id")
    op.drop_column("chats", "kind")
