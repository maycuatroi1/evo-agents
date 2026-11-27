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
Revision 0164 gives chats ``label_override``, the level a project admin set for its label (``chats.service.labels``).

Revision 0165 bridges a group chat to groups of a core plugin's channel
(``evo_agents.hub.server.chats.service.bridges``): chat_bridges holds each link (the plugin, the kind of channel, the
group's id there, the config and state the plugin keeps, who linked it and who unlinked it, from where),
chat_bridge_codes the one-time codes a chat admin links one with, chat_bridge_messages the group's id of each message a
bridge carried in or out; chat_messages takes ``via``, the kind of channel a message came in by. ``bridge_tables`` adds
them.

Revision 0213 gives chat_messages ``from_output_id``, the interactive answer (an output of a run) a person sent the
message from, which the message keeps as its source and the hub's rate limit counts (``chats.service.answers``).

Revision 0229 adds chat_label_shares: a person of a chat who clears a project's part of its label because a writer on
that project shared it with them there (``shared_by``), not by a grant of their own (``chats.service.access``). ``share_table``
adds it.

Revision 0232 gives chats ``opened_from_message_id``: for a group chat a direct chat opened (a mention of an agent the
direct chat does not have, ``evo_agents.hub.server.chats.service.groups``), the first message of the direct chat's
thread it came from, one group per thread (chats_opened_from_key).

Revision 0234 lets chats ``kind`` be work (``chats.WORK``, a group chat the hub opens for a trace of an agent system)
and gives chat_messages ``copied_from_id``: the message of another chat a message copies, between a work chat and the
root thread of its trace (``evo_agents.hub.server.work_chats``), which hands no work again.

Revision 0266 gives chat_messages ``guest_name``: the name of its author when that is a guest, a sender of a bridged
group who linked no account of the hub (``core_plugin_api.bridge_guest``). At most one of ``author_user_id``,
``author_profile_id`` and ``guest_name`` is set; none is the hub.

Revision 0270 gives chat_messages ``repeats_id``: for a line of the hub in a work chat that repeats a question or an
answer an agent wrote there ("asked ... (run #N)", "answered (run #N)"), the message it repeats
(``evo_agents.hub.server.work_chats.service.say``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from evo_agents.hub.columns import fkey, id_column, stamp


def _chats(metadata: MetaData) -> tuple[Table, Table]:
    chats = Table(
        "chats",
        metadata,
        id_column(),
        Column("title", Text, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        stamp("created_at"),
        Column("event_seq", BigInteger, nullable=False, server_default="0"),
        Column("kind", Text, nullable=False, server_default="group"),  # 0050
        Column("direct_user_id", BigInteger),  # 0050
        Column("direct_profile_id", BigInteger),  # 0050
        Column("image_sha256", Text),  # 0051
        Column("image_size", BigInteger),  # 0051
        Column("label_override", JSONB),  # 0164: the level a project admin set, and the projects it lowers
        Column("opened_from_message_id", BigInteger),  # 0232: the direct chat's thread a group was opened from
        fkey("chats", "created_by", "users.id", ondelete="RESTRICT"),
        fkey("chats", "direct_user_id", "users.id", ondelete="RESTRICT"),
        fkey("chats", "direct_profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        fkey("chats", "opened_from_message_id", "chat_messages.id", ondelete="SET NULL", use_alter=True),  # 0232
        PrimaryKeyConstraint("id", name="chats_pkey"),
        UniqueConstraint("direct_user_id", "direct_profile_id", name="chats_direct_key"),
    )
    Index("chats_image_sha256_idx", chats.c.image_sha256, postgresql_where=chats.c.image_sha256.is_not(None))  # 0051
    Index(  # 0232
        "chats_opened_from_key",
        chats.c.opened_from_message_id,
        unique=True,
        postgresql_where=chats.c.opened_from_message_id.is_not(None),
    )
    members = Table(
        "chat_members",
        metadata,
        id_column(),
        Column("chat_id", BigInteger, nullable=False),
        Column("user_id", BigInteger),
        Column("profile_id", BigInteger),
        Column("added_by", BigInteger),
        Column("added_by_run_id", BigInteger),
        stamp("added_at"),
        Column("removed_at", DateTime(timezone=True)),
        Column("last_read_seq", BigInteger, nullable=False, server_default="0"),
        fkey("chat_members", "chat_id", "chats.id", ondelete="CASCADE"),
        fkey("chat_members", "user_id", "users.id", ondelete="CASCADE"),
        fkey("chat_members", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        fkey("chat_members", "added_by", "users.id", ondelete="RESTRICT"),
        fkey("chat_members", "added_by_run_id", "runs.id", ondelete="SET NULL"),
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
        id_column(),
        Column("chat_id", BigInteger, nullable=False),
        Column("seq", BigInteger, nullable=False),
        Column("thread_root_id", BigInteger),
        Column("author_user_id", BigInteger),
        Column("author_profile_id", BigInteger),
        Column("run_id", BigInteger),
        Column("body", Text, nullable=False),
        Column("label", JSONB, nullable=False),
        stamp("created_at"),
        Column("via", Text),  # 0165: the kind of channel it came in by, from a bridged group
        Column("from_output_id", BigInteger),  # 0213: the interactive answer a person sent it from
        Column("copied_from_id", BigInteger),  # 0234: the message of another chat it copies, which hands no work again
        Column("guest_name", Text),  # 0266: its author, a bridged group's sender who linked no account of the hub
        Column("repeats_id", BigInteger),  # 0270: the message of its work chat a line of the hub repeats
        fkey("chat_messages", "chat_id", "chats.id", ondelete="CASCADE"),
        fkey("chat_messages", "thread_root_id", "chat_messages.id", ondelete="CASCADE"),
        fkey("chat_messages", "author_user_id", "users.id", ondelete="RESTRICT"),
        fkey("chat_messages", "author_profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        fkey("chat_messages", "run_id", "runs.id", ondelete="SET NULL"),
        fkey("chat_messages", "from_output_id", "run_outputs.id", ondelete="SET NULL", use_alter=True),  # 0213
        fkey("chat_messages", "copied_from_id", "chat_messages.id", ondelete="SET NULL"),  # 0234
        fkey("chat_messages", "repeats_id", "chat_messages.id", ondelete="SET NULL"),  # 0270
        PrimaryKeyConstraint("id", name="chat_messages_pkey"),
        UniqueConstraint("chat_id", "seq", name="chat_messages_seq_key"),
    )
    Index(
        "chat_messages_thread_idx",
        messages.c.thread_root_id,
        messages.c.id,
        postgresql_where=messages.c.thread_root_id.is_not(None),
    )
    Index(  # 0213: the messages a person sent from interactive answers, which the hub's rate limit counts
        "chat_messages_from_answer_idx",
        messages.c.author_user_id,
        messages.c.created_at,
        postgresql_where=messages.c.from_output_id.is_not(None),
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
        fkey("chat_mentions", "message_id", "chat_messages.id", ondelete="CASCADE"),
        fkey("chat_mentions", "user_id", "users.id", ondelete="CASCADE"),
        fkey("chat_mentions", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
        fkey("chat_mentions", "run_id", "runs.id", ondelete="SET NULL"),
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
        stamp("created_at"),
        fkey("chat_reactions", "message_id", "chat_messages.id", ondelete="CASCADE"),
        fkey("chat_reactions", "user_id", "users.id", ondelete="CASCADE"),
        fkey("chat_reactions", "profile_id", "agent_profiles.id", ondelete="RESTRICT"),
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
        stamp("created_at"),
        fkey("chat_events", "chat_id", "chats.id", ondelete="CASCADE"),
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
    runs.append_constraint(fkey("runs", "chat_id", "chats.id", ondelete="RESTRICT"))
    # chat_messages.run_id points back at runs: this key is made apart from the tables, so that they sort
    runs.append_constraint(fkey("runs", "chat_message_id", "chat_messages.id", ondelete="RESTRICT", use_alter=True))
    Index("runs_chat_idx", runs.c.chat_id, runs.c.queued_at, postgresql_where=runs.c.chat_id.is_not(None))


def chat_tables(metadata: MetaData, runs: Table) -> tuple[Table, Table, Table, Table, Table, Table]:
    """(chats, chat_members, chat_messages, chat_mentions, chat_reactions, chat_events), added to ``metadata``; the
    columns of a chat run added to ``runs``."""
    found = (*_chats(metadata), *_messages(metadata), *_reactions(metadata))
    _chat_runs(runs)
    return found


def share_table(metadata: MetaData) -> Table:
    """chat_label_shares of revision 0229, added to ``metadata``: one row per chat, project and person it was shared
    with."""
    return Table(
        "chat_label_shares",
        metadata,
        Column("chat_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("shared_by", BigInteger, nullable=False),
        stamp("shared_at"),
        fkey("chat_label_shares", "chat_id", "chats.id", ondelete="CASCADE"),
        fkey("chat_label_shares", "project_id", "projects.id", ondelete="CASCADE"),
        fkey("chat_label_shares", "user_id", "users.id", ondelete="CASCADE"),
        fkey("chat_label_shares", "shared_by", "users.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("chat_id", "project_id", "user_id", name="chat_label_shares_pkey"),
    )


def bridge_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(chat_bridges, chat_bridge_codes, chat_bridge_messages) of revision 0165, added to ``metadata``."""
    bridges = Table(
        "chat_bridges",
        metadata,
        id_column(),
        Column("chat_id", BigInteger, nullable=False),
        Column("plugin", Text, nullable=False),  # the id of the core plugin that contributes the kind
        Column("kind", Text, nullable=False),
        Column("external_id", Text, nullable=False),  # the group's id in the channel
        Column("config", JSONB, nullable=False, server_default="{}"),
        Column("state", JSONB, nullable=False, server_default="{}"),
        Column("linked_by", BigInteger),
        stamp("linked_at"),
        Column("unlinked_at", DateTime(timezone=True)),
        Column("unlinked_by", BigInteger),
        Column("unlinked_via", Text),  # web, cli, bot or admin
        fkey("chat_bridges", "chat_id", "chats.id", ondelete="CASCADE"),
        fkey("chat_bridges", "linked_by", "users.id", ondelete="SET NULL"),
        fkey("chat_bridges", "unlinked_by", "users.id", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="chat_bridges_pkey"),
    )
    linked = bridges.c.unlinked_at.is_(None)
    Index("chat_bridges_external_key", bridges.c.kind, bridges.c.external_id, unique=True, postgresql_where=linked)
    Index("chat_bridges_chat_idx", bridges.c.chat_id, postgresql_where=linked)
    codes = Table(
        "chat_bridge_codes",
        metadata,
        id_column(),
        Column("chat_id", BigInteger, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("token_id", BigInteger, nullable=False),  # the web session that made it
        Column("kind", Text, nullable=False),
        Column("code_hash", Text, nullable=False),
        stamp("created_at"),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("used_at", DateTime(timezone=True)),
        fkey("chat_bridge_codes", "chat_id", "chats.id", ondelete="CASCADE"),
        fkey("chat_bridge_codes", "user_id", "users.id", ondelete="CASCADE"),
        fkey("chat_bridge_codes", "token_id", "tokens.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="chat_bridge_codes_pkey"),
        UniqueConstraint("code_hash", name="chat_bridge_codes_code_hash_key"),
    )
    Index("chat_bridge_codes_user_idx", codes.c.user_id)
    carried = Table(
        "chat_bridge_messages",
        metadata,
        Column("bridge_id", BigInteger, nullable=False),
        Column("message_id", BigInteger, nullable=False),
        Column("direction", Text, nullable=False),  # in: it came from the group; out: it went there
        Column("external_id", Text, nullable=False),  # the group's id of the message
        stamp("created_at"),
        fkey("chat_bridge_messages", "bridge_id", "chat_bridges.id", ondelete="CASCADE"),
        fkey("chat_bridge_messages", "message_id", "chat_messages.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("bridge_id", "message_id", "direction", name="chat_bridge_messages_pkey"),
        UniqueConstraint("bridge_id", "external_id", name="chat_bridge_messages_external_key"),
    )
    Index("chat_bridge_messages_message_idx", carried.c.message_id)
    return bridges, codes, carried
