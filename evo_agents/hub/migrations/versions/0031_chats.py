"""Group chats: people and agents of projects in one conversation, their messages with threads and mentions, the
reactions on them, and the events a chat's stream sends.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-14

``evo_agents.hub.chats`` describes the design; the bounds and lists below are copies of its constants and of
``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables_chats`` describes the
tables.

chats holds each chat: its title (1 to 120 characters), who created it and when, and the number of its last event.
chat_members holds who is in it, a person or an agent (exactly one of user_id and profile_id), once each, with who
added them (a person, or later the run of an agent), when, when they left and how far the person read. chat_messages
holds the messages: the chat event that brought each (unique in the chat), the first message of its thread (never
itself), its author (a person, an agent, or the hub: at most one of the two), the run of an agent that wrote it, its
body (1 to 16384 bytes of UTF-8) and its label, a JSON array. chat_mentions holds what a message mentions, as the hub
read it when it was sent: the name (1 to 100 characters), how it reached a member (login, handle, project or thread),
the person or agent it reached (at most one) and the run it made, or why it reached none. chat_reactions holds one
emoji (1 to 16 characters) of exactly one person or agent on a message, once each. chat_events holds what happened in
a chat in order (a message, a reaction put or taken off, a member added or removed), each a JSON object.

notifications.notice_kind takes two more values: 'chat_mention' (someone mentioned the member in a chat) and
'chat_added' (someone added the member to a chat).

Going back deletes the chat notices, puts the notice kinds of 0030 back and drops the six tables, which is the schema
that schema 0030 runs on; every chat is lost.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

NOTICE_KINDS_0030 = (
    "push_default_branch",
    "merge_default_branch",
    "plan_finished",
    "run_failed",
    "curator_brief",
    "curator_paused",
    "author_waiting",
)
CHAT_NOTICES = ("chat_mention", "chat_added")
NOTICE_KINDS = (*NOTICE_KINDS_0030, *CHAT_NOTICES)  # evo_agents.hub.runs.NOTICE_KINDS
MAX_TITLE_CHARS = 120  # evo_agents.hub.chats.MAX_TITLE_CHARS
MAX_MESSAGE_BYTES = 16 * 1024  # evo_agents.hub.chats.MAX_MESSAGE_BYTES
MAX_NAME_CHARS = 100  # evo_agents.hub.chats.MAX_NAME_CHARS
MAX_EMOJI_CHARS = 16  # evo_agents.hub.chats.MAX_EMOJI_CHARS
VIA = ("login", "handle", "project", "thread")  # evo_agents.hub.chats.VIA
EVENT_KINDS = ("message", "reaction_added", "reaction_removed", "member_added", "member_removed")
TABLES = ("chat_events", "chat_reactions", "chat_mentions", "chat_messages", "chat_members", "chats")  # drop order

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def _fk(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _one_of(table: str, first: str, second: str, *, exactly: bool) -> sa.CheckConstraint:
    """Exactly one of the two columns set, or at most one."""
    if exactly:
        rule = col(first).is_(None) != col(second).is_(None)
    else:
        rule = sa.or_(col(first).is_(None), col(second).is_(None))
    return sa.CheckConstraint(rule, name=f"{table}_who_check")


def _json(table: str, column: str, kind: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sa.func.jsonb_typeof(col(column)) == kind, name=f"{table}_{column}_check")


def _create_chats() -> None:
    op.create_table(
        "chats",
        _id(),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.Column("event_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(sa.func.char_length(col("title")).between(1, MAX_TITLE_CHARS), name="chats_title_check"),
        sa.CheckConstraint(col("event_seq") >= 0, name="chats_event_seq_check"),
        _fk("chats", "created_by", "users.id", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="chats_pkey"),
    )
    op.create_table(
        "chat_members",
        _id(),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("added_by_run_id", sa.BigInteger(), nullable=True),
        _stamp("added_at"),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_read_seq", sa.BigInteger(), server_default="0", nullable=False),
        _one_of("chat_members", "user_id", "profile_id", exactly=True),
        sa.CheckConstraint(col("last_read_seq") >= 0, name="chat_members_last_read_check"),
        _fk("chat_members", "chat_id", "chats.id", ondelete="CASCADE"),
        _fk("chat_members", "user_id", "users.id", ondelete="CASCADE"),
        _fk("chat_members", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        _fk("chat_members", "added_by", "users.id", ondelete="RESTRICT"),
        _fk("chat_members", "added_by_run_id", "runs.id", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="chat_members_pkey"),
        sa.UniqueConstraint("chat_id", "user_id", name="chat_members_person_key"),
        sa.UniqueConstraint("chat_id", "profile_id", name="chat_members_agent_key"),
    )
    op.create_index("chat_members_user_idx", "chat_members", ["user_id"], postgresql_where=col("removed_at").is_(None))


def _create_messages() -> None:
    op.create_table(
        "chat_messages",
        _id(),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("thread_root_id", sa.BigInteger(), nullable=True),
        sa.Column("author_user_id", sa.BigInteger(), nullable=True),
        sa.Column("author_profile_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("label", JSONB(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(
            sa.func.octet_length(col("body")).between(1, MAX_MESSAGE_BYTES), name="chat_messages_body_check"
        ),
        sa.CheckConstraint(col("thread_root_id") != col("id"), name="chat_messages_thread_check"),
        _one_of("chat_messages", "author_user_id", "author_profile_id", exactly=False),
        _json("chat_messages", "label", "array"),
        _fk("chat_messages", "chat_id", "chats.id", ondelete="CASCADE"),
        _fk("chat_messages", "thread_root_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("chat_messages", "author_user_id", "users.id", ondelete="RESTRICT"),
        _fk("chat_messages", "author_profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        _fk("chat_messages", "run_id", "runs.id", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="chat_messages_pkey"),
        sa.UniqueConstraint("chat_id", "seq", name="chat_messages_seq_key"),
    )
    op.create_index(
        "chat_messages_thread_idx",
        "chat_messages",
        ["thread_root_id", "id"],
        postgresql_where=col("thread_root_id").is_not(None),
    )
    op.create_table(
        "chat_mentions",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("via", sa.Text(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("refusal", sa.Text(), nullable=True),
        sa.CheckConstraint(
            sa.func.char_length(col("name")).between(1, MAX_NAME_CHARS), name="chat_mentions_name_check"
        ),
        sa.CheckConstraint(sa.or_(col("via").is_(None), col("via").in_(VIA)), name="chat_mentions_via_check"),
        sa.CheckConstraint(col("ordinal") >= 0, name="chat_mentions_ordinal_check"),
        _one_of("chat_mentions", "user_id", "profile_id", exactly=False),
        _fk("chat_mentions", "message_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("chat_mentions", "user_id", "users.id", ondelete="CASCADE"),
        _fk("chat_mentions", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        _fk("chat_mentions", "run_id", "runs.id", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("message_id", "ordinal", name="chat_mentions_pkey"),
    )


def _create_reactions() -> None:
    op.create_table(
        "chat_reactions",
        _id(),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("emoji", sa.Text(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(
            sa.func.char_length(col("emoji")).between(1, MAX_EMOJI_CHARS), name="chat_reactions_emoji_check"
        ),
        _one_of("chat_reactions", "user_id", "profile_id", exactly=True),
        _fk("chat_reactions", "message_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("chat_reactions", "user_id", "users.id", ondelete="CASCADE"),
        _fk("chat_reactions", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="chat_reactions_pkey"),
        sa.UniqueConstraint("message_id", "user_id", "emoji", name="chat_reactions_person_key"),
        sa.UniqueConstraint("message_id", "profile_id", "emoji", name="chat_reactions_agent_key"),
    )
    op.create_table(
        "chat_events",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(col("kind").in_(EVENT_KINDS), name="chat_events_kind_check"),
        sa.CheckConstraint(col("seq") >= 1, name="chat_events_seq_check"),
        _json("chat_events", "body", "object"),
        _fk("chat_events", "chat_id", "chats.id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id", "seq", name="chat_events_pkey"),
    )


def _notice_kinds(kinds) -> None:
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    _create_chats()
    _create_messages()
    _create_reactions()
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    _notice_kinds(NOTICE_KINDS)


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.execute(notifications.delete().where(notifications.c.notice_kind.in_(CHAT_NOTICES)))
    _notice_kinds(NOTICE_KINDS_0030)
    op.drop_index("chat_messages_thread_idx", table_name="chat_messages")
    op.drop_index("chat_members_user_idx", table_name="chat_members")
    for name in TABLES:
        op.drop_table(name)
