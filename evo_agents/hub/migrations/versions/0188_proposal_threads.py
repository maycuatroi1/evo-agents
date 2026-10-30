"""The threads of the Curator's proposals the project's agent answers.

Revision ID: 0188
Revises: 0187
Create Date: 2026-10-31

proposal_threads is new (plan proposals-answer-themselves): one row for each proposal of the Curator whose project's
charter has ``answer_by`` agent, the thread the hub opened for it in the project's triage chat
(``evo_agents.hub.server.curator_threads``). It names the proposal (one row each,
``proposal_threads_proposal_id_key``), the chat and the first message of the thread (one proposal a thread,
``proposal_threads_root_key``), the chat run the hub's latest mention of the agent made, and when the thread was opened
and the agent last asked.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0188"
down_revision = "0187"
branch_labels = None
depends_on = None

TABLE = "proposal_threads"


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("proposal_id", "proposals", ondelete="CASCADE"),
        _key("chat_id", "chats", ondelete="CASCADE"),
        _key("root_message_id", "chat_messages", ondelete="CASCADE"),
        _key("agent_run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("proposal_id", name=f"{TABLE}_proposal_id_key"),
        sa.UniqueConstraint("chat_id", "root_message_id", name=f"{TABLE}_root_key"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
