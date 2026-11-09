"""A reply to an agent's message wakes it: chat_mentions.via takes ``reply``.

Revision ID: 0228
Revises: 0227
Create Date: 2026-11-09

In a group chat, a person's message that mentions no one and answers a message an agent of the chat wrote is work for
that agent (``evo_agents.hub.chats``): the mention that hands it the work reaches it ``via`` reply
(chat_mentions_via_check).

Going back reads a mention ``via`` reply as ``via`` thread, how a message without an @ reached the agent of the
sender's session in a thread before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0228"
down_revision = "0227"
branch_labels = None
depends_on = None

VIA_0213 = ("login", "handle", "project", "thread", "direct", "settle", "answer")
VIA = (*VIA_0213, "reply")  # evo_agents.hub.chats.VIA

col = sa.column


def _create_via(values) -> None:
    rule = sa.or_(col("via").is_(None), col("via").in_(values))
    op.create_check_constraint("chat_mentions_via_check", "chat_mentions", rule)


def upgrade() -> None:
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    _create_via(VIA)


def downgrade() -> None:
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    mentions = sa.table("chat_mentions", col("via"))
    op.execute(sa.update(mentions).where(mentions.c.via == "reply").values(via="thread"))
    _create_via(VIA_0213)
