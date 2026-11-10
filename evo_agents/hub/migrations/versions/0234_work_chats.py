"""Work chats: one chat of kind work for each trace of an agent system with a hand-off.

Revision ID: 0234
Revises: 0233
Create Date: 2026-11-10

chats.kind takes ``work`` (``evo_agents.hub.chats.WORK``): a group chat the hub opens at the first hand-off of a trace
(an agent's ask, an escalation, a thread to the Orchestrator), naming neither person nor agent on its row, so
chats_direct_check takes it as it takes a group chat. system_traces.work_chat_id names it; NULL: a trace with no
hand-off yet, as every trace before. chat_messages.copied_from_id names the message of another chat a message copies
(the coordinator's messages in the root thread, a person's message for the coordinator), which hands no work again;
NULL: a message no one copied. decisions.awaits_run_id names the run asked whose end answers a wait of an agent ask, so
two asks that reach one session are both answered; NULL: any other decision, and the waits before, which
runs.spawned_wait_id answers. Every column allows NULL and has no default, so the code before it runs on the schema
after it; the row they name going sets them NULL.

The names below are copies of ``evo_agents.hub.chats`` as of this revision, never imports.

Going back makes every work chat a group chat of the same members and drops the three columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0234"
down_revision = "0233"
branch_labels = None
depends_on = None

GROUP, DIRECT, SETTLE, WORK = "group", "direct", "settle", "work"  # evo_agents.hub.chats.CHAT_KINDS

col = sa.column
COLUMNS = (
    ("system_traces", "work_chat_id", "chats"),
    ("chat_messages", "copied_from_id", "chat_messages"),
    ("decisions", "awaits_run_id", "runs"),
)


def _kinds(kinds) -> None:
    """chats_kind_check and chats_direct_check for ``kinds``: a group or a work chat names neither person nor agent, a
    direct chat both, a settle chat its agent alone."""
    op.drop_constraint("chats_direct_check", "chats", type_="check")
    op.drop_constraint("chats_kind_check", "chats", type_="check")
    op.create_check_constraint("chats_kind_check", "chats", col("kind").in_(kinds))
    user, profile = col("direct_user_id"), col("direct_profile_id")
    pairs = [
        sa.and_(col("kind").in_([kind for kind in (GROUP, WORK) if kind in kinds]), user.is_(None), profile.is_(None)),
        sa.and_(col("kind") == DIRECT, user.is_not(None), profile.is_not(None)),
        sa.and_(col("kind") == SETTLE, user.is_(None), profile.is_not(None)),
    ]
    op.create_check_constraint("chats_direct_check", "chats", sa.or_(*pairs))


def upgrade() -> None:
    _kinds((GROUP, DIRECT, SETTLE, WORK))
    for table, column, target in COLUMNS:
        op.add_column(table, sa.Column(column, sa.BigInteger(), nullable=True))
        op.create_foreign_key(f"{table}_{column}_fkey", table, target, [column], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    for table, column, _ in COLUMNS:
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.drop_column(table, column)
    chats = sa.table("chats", col("kind"))
    op.execute(chats.update().where(col("kind") == WORK).values(kind=GROUP))
    _kinds((GROUP, DIRECT, SETTLE))
