"""Interactive answers in chats: run_outputs.interactive and chat_messages.from_output_id.

Revision ID: 0213
Revises: 0212
Create Date: 2026-11-06

An agent marks an HTML file it sends as an interactive answer (``evo-agents worker output --interactive``): the hub
keeps the mark on the output, and serves it in a chat that is not restricted under a policy that loads libraries from
a fixed list of CDNs (``evo_agents.hub.outputs.INTERACTIVE_POLICY``). A message a person sends from such an answer
(``POST /v1/chats/{id}/messages`` with ``from_answer``) keeps the output it came from, which also counts those messages
of a person for the hub's rate limit (chat_messages_from_answer_idx); the mention that hands it to the agent that
wrote the answer reaches it ``via`` answer (chat_mentions_via_check).

Going back drops both columns and the index, and a mention ``via`` answer reads as ``via`` thread, how a message
without an @ reached the agent of the sender's session in a thread before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0213"
down_revision = "0212"
branch_labels = None
depends_on = None

VIA_0070 = ("login", "handle", "project", "thread", "direct", "settle")
VIA = (*VIA_0070, "answer")  # evo_agents.hub.chats.VIA

col = sa.column


def _create_via(values) -> None:
    rule = sa.or_(col("via").is_(None), col("via").in_(values))
    op.create_check_constraint("chat_mentions_via_check", "chat_mentions", rule)


def upgrade() -> None:
    op.add_column("run_outputs", sa.Column("interactive", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("chat_messages", sa.Column("from_output_id", sa.BigInteger()))
    op.create_foreign_key(
        "chat_messages_from_output_id_fkey",
        "chat_messages",
        "run_outputs",
        ["from_output_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "chat_messages_from_answer_idx",
        "chat_messages",
        ["author_user_id", "created_at"],
        postgresql_where=col("from_output_id").is_not(None),
    )
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    _create_via(VIA)


def downgrade() -> None:
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    mentions = sa.table("chat_mentions", col("via"))
    op.execute(sa.update(mentions).where(mentions.c.via == "answer").values(via="thread"))
    _create_via(VIA_0070)
    op.drop_index("chat_messages_from_answer_idx", table_name="chat_messages")
    op.drop_constraint("chat_messages_from_output_id_fkey", "chat_messages", type_="foreignkey")
    op.drop_column("chat_messages", "from_output_id")
    op.drop_column("run_outputs", "interactive")
