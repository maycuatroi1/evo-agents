"""The tables of group chats (0031), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.chats``, ``tables.chat_messages`` and the rest): queries name them there.

chats holds each chat: its title, who created it and when, and ``event_seq``, the number of its last event.
chat_members holds who is in it, a person (``user_id``) or an agent (``profile_id``), one row each, ``removed_at`` set
once they left; ``last_read_seq`` is how far the person read. chat_messages holds the messages, each numbered by the
chat event that brought it (``seq``), the first message of its thread (``thread_root_id``, NULL for one that starts
none), its author (a person, an agent, or the hub when both are NULL), the run that wrote it (an agent's), its body and
its label: the labels of the projects of the chat's agents when it was sent, as a run's events read as its project's.
chat_mentions holds the names a message mentions as the hub read them when it was sent, with the member each reached
and how, or why it reached none. chat_reactions holds one row per emoji of a person or an agent on a message.
chat_events holds what happened in a chat in order, which its stream sends: a message, a reaction put or taken off, a
member added or removed.

Revision 0032 gives runs the columns of a chat run (``evo_agents.hub.chat_runs``): ``chat_id``, ``chat_message_id``
(the message that started it) and ``hop``; ``chat_tables`` adds them, their foreign keys and the index of a chat's runs
to the runs table it is given, so that ``evo_agents.hub.tables`` describes runs whole. Revision 0049 gives runs ``seen_audience``, the principals of what the
hub returned to the run's agent with an audience (``evo_agents.hub.server.run_audience``), which ``chat_tables`` adds too.
Revision 0050 gives chats ``kind`` (``chats.CHAT_KINDS``: group, or direct) and, for a direct chat, its person
(``direct_user_id``) and its agent (``direct_profile_id``), one direct chat per pair (chats_direct_key). Revision 0051
gives a group chat its image: the SHA-256 and the size of a blob of the blob store (``image_sha256``, ``image_size``),
which the chat's row refers to, as ``blob_gc`` counts it, so that the collection of unreferenced blobs leaves it.
Revision 0072 drops ``budget_usd``, the cost a chat's runs could reach in 24 hours, which 0032 gave chats.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _id() -> Column:
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def _fk(table: str, column: str, target: str, *, ondelete: str, use_alter: bool = False) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        [column], [target], ondelete=ondelete, name=f"{table}_{column}_fkey", use_alter=use_alter
    )


def _chats(metadata: MetaData) -> tuple[Table, Table]:
    chats = Table(
        "chats",
        metadata,
        _id(),
        Column("title", Text, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        Column("event_seq", BigInteger, nullable=False, server_default="0"),
        Column("kind", Text, nullable=False, server_default="group"),  # 0050
        Column("direct_user_id", BigInteger),  # 0050
        Column("direct_profile_id", BigInteger),  # 0050
        Column("image_sha256", Text),  # 0051
        Column("image_size", BigInteger),  # 0051
        _fk("chats", "created_by", "users.id", ondelete="RESTRICT"),
        _fk("chats", "direct_user_id", "users.id", ondelete="RESTRICT"),
        _fk("chats", "direct_profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="chats_pkey"),
        UniqueConstraint("direct_user_id", "direct_profile_id", name="chats_direct_key"),
    )
    Index("chats_image_sha256_idx", chats.c.image_sha256, postgresql_where=chats.c.image_sha256.is_not(None))  # 0051
    members = Table(
        "chat_members",
        metadata,
        _id(),
        Column("chat_id", BigInteger, nullable=False),
        Column("user_id", BigInteger),
        Column("profile_id", BigInteger),
        Column("added_by", BigInteger),
        Column("added_by_run_id", BigInteger),
        _stamp("added_at"),
        Column("removed_at", DateTime(timezone=True)),
        Column("last_read_seq", BigInteger, nullable=False, server_default="0"),
        _fk("chat_members", "chat_id", "chats.id", ondelete="CASCADE"),
        _fk("chat_members", "user_id", "users.id", ondelete="CASCADE"),
        _fk("chat_members", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        _fk("chat_members", "added_by", "users.id", ondelete="RESTRICT"),
        _fk("chat_members", "added_by_run_id", "runs.id", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="chat_members_pkey"),
        UniqueConstraint("chat_id", "user_id", name="chat_members_person_key"),
        UniqueConstraint("chat_id", "profile_id", name="chat_members_agent_key"),
    )
    Index("chat_members_user_idx", members.c.user_id, postgresql_where=members.c.removed_at.is_(None))
    return chats, members


def _messages(metadata: MetaData) -> tuple[Table, Table]:
    messages = Table(
        "chat_messages",
        metadata,
        _id(),
        Column("chat_id", BigInteger, nullable=False),
        Column("seq", BigInteger, nullable=False),
        Column("thread_root_id", BigInteger),
        Column("author_user_id", BigInteger),
        Column("author_profile_id", BigInteger),
        Column("run_id", BigInteger),
        Column("body", Text, nullable=False),
        Column("label", JSONB, nullable=False),
        _stamp("created_at"),
        _fk("chat_messages", "chat_id", "chats.id", ondelete="CASCADE"),
        _fk("chat_messages", "thread_root_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("chat_messages", "author_user_id", "users.id", ondelete="RESTRICT"),
        _fk("chat_messages", "author_profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        _fk("chat_messages", "run_id", "runs.id", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="chat_messages_pkey"),
        UniqueConstraint("chat_id", "seq", name="chat_messages_seq_key"),
    )
    Index(
        "chat_messages_thread_idx",
        messages.c.thread_root_id,
        messages.c.id,
        postgresql_where=messages.c.thread_root_id.is_not(None),
    )
    mentions = Table(
        "chat_mentions",
        metadata,
        Column("message_id", BigInteger, nullable=False),
        Column("ordinal", SmallInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("via", Text),
        Column("user_id", BigInteger),
        Column("profile_id", BigInteger),
        Column("run_id", BigInteger),
        Column("refusal", Text),
        _fk("chat_mentions", "message_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("chat_mentions", "user_id", "users.id", ondelete="CASCADE"),
        _fk("chat_mentions", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        _fk("chat_mentions", "run_id", "runs.id", ondelete="SET NULL"),
        PrimaryKeyConstraint("message_id", "ordinal", name="chat_mentions_pkey"),
    )
    return messages, mentions


def _reactions(metadata: MetaData) -> tuple[Table, Table]:
    reactions = Table(
        "chat_reactions",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("message_id", BigInteger, nullable=False),
        Column("user_id", BigInteger),
        Column("profile_id", BigInteger),
        Column("emoji", Text, nullable=False),
        _stamp("created_at"),
        _fk("chat_reactions", "message_id", "chat_messages.id", ondelete="CASCADE"),
        _fk("chat_reactions", "user_id", "users.id", ondelete="CASCADE"),
        _fk("chat_reactions", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="chat_reactions_pkey"),
        UniqueConstraint("message_id", "user_id", "emoji", name="chat_reactions_person_key"),
        UniqueConstraint("message_id", "profile_id", "emoji", name="chat_reactions_agent_key"),
    )
    events = Table(
        "chat_events",
        metadata,
        Column("chat_id", BigInteger, nullable=False),
        Column("seq", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("body", JSONB, nullable=False),
        _stamp("created_at"),
        _fk("chat_events", "chat_id", "chats.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("chat_id", "seq", name="chat_events_pkey"),
    )
    return reactions, events


def _chat_runs(runs: Table) -> None:
    """The columns of a chat run (0032) added to ``runs``: its chat, the message that started it and its hop; and the
    audience the agent of any run was shown (0049)."""
    runs.append_column(Column("chat_id", BigInteger))
    runs.append_column(Column("chat_message_id", BigInteger))
    runs.append_column(Column("hop", SmallInteger))
    runs.append_column(Column("seen_audience", ARRAY(Text), nullable=False, server_default="{}"))  # 0049
    runs.append_constraint(_fk("runs", "chat_id", "chats.id", ondelete="RESTRICT"))
    # chat_messages.run_id points back at runs: this key is made apart from the tables, so that they sort
    runs.append_constraint(_fk("runs", "chat_message_id", "chat_messages.id", ondelete="RESTRICT", use_alter=True))
    Index("runs_chat_idx", runs.c.chat_id, runs.c.queued_at, postgresql_where=runs.c.chat_id.is_not(None))


def chat_tables(metadata: MetaData, runs: Table) -> tuple[Table, Table, Table, Table, Table, Table]:
    """(chats, chat_members, chat_messages, chat_mentions, chat_reactions, chat_events), added to ``metadata``; the
    columns of a chat run added to ``runs``."""
    found = (*_chats(metadata), *_messages(metadata), *_reactions(metadata))
    _chat_runs(runs)
    return found
