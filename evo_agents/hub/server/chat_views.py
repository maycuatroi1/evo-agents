"""What the chat API answers (``evo_agents.hub.server.chats``): a chat with its people and agents, its messages with
their authors, mentions, reactions and replies, and the events its stream sends, read from the tables of
``tables_chats``.

An agent shows with its name, colour and avatar as its profile has them now, deleted or not: a soft-deleted profile
takes no new work and keeps its name and avatar on what it wrote, flagged ``deleted``. A message's reactions are
counted by emoji, in the order each emoji was first put, with who put it and whether the reader did.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.agent_profiles import icon_url
from evo_agents.hub.server.chat_access import ChatAccess, LabelReader
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select

HUB_NAME = "hub"


# Models


class ChatAuthor(BaseModel):
    """Who wrote a message or put a reaction: a person, an agent or the hub itself."""

    kind: Literal["person", "agent", "hub"]
    login: str | None = Field(None, description="a person's")
    handle: str | None = Field(None, description="an agent's, @ left out")
    project: str | None = Field(None, description="an agent's project")
    name: str = Field(description="what the web shows: a person's login, an agent's name, or hub")
    color: str | None = Field(None, description="an agent's colour, #rrggbb")
    icon_url: str | None = Field(None, description="an agent's avatar on this hub; null: its initial on its colour")
    deleted: bool = Field(False, description="an agent deleted since: it takes no new work, and keeps its name")


class ChatPerson(BaseModel):
    login: str
    added_by: str | None
    added_at: datetime


class ChatAgent(BaseModel):
    handle: str
    project: str
    name: str
    color: str
    icon_url: str | None
    is_default: bool = Field(description="the project's default agent, which @<project> names")
    deleted: bool = Field(description="deleted since it was added: it takes no new work")
    added_by: str | None
    added_at: datetime


class ChatMention(BaseModel):
    """A name a message mentions, as the hub read it when the message was sent."""

    name: str = Field(description="as written, lowered, @ left out")
    via: Literal[chats.VIA] | None = Field(description="how it reached a member; null: it reached none")
    login: str | None = Field(description="the person it reached")
    handle: str | None = Field(description="the agent it reached, or would have")
    refusal: str | None = Field(
        description="why it reached no member (not in the chat, deleted), or why the agent it reached took no work"
    )
    run_id: int | None = Field(None, description="the run of the agent the mention handed work to, or woke")


class ChatReaction(BaseModel):
    emoji: str
    count: int
    mine: bool = Field(description="whether the reader put it")
    people: list[str] = Field(description="the logins of the people who put it, first first")
    agents: list[str] = Field(description="the handles of the agents that put it")


class GroupMessage(BaseModel):
    id: int
    chat_id: int
    seq: int = Field(description="the chat event that brought it: events?after= and Last-Event-ID take it")
    thread_root_id: int | None = Field(description="the first message of its thread; null when it starts none")
    reply_count: int = Field(description="the messages of the thread it starts")
    author: ChatAuthor
    run_id: int | None = Field(description="the run of the agent that wrote it")
    body: str
    mentions: list[ChatMention]
    reactions: list[ChatReaction]
    label: list[dict] = Field(description="the labels of the projects of the chat's agents when it was sent")
    created_at: datetime


class Chat(BaseModel):
    id: int
    title: str
    created_by: str
    created_at: datetime
    people: list[ChatPerson]
    agents: list[ChatAgent]
    label: list[dict] = Field(description="the labels of its agents' projects: every person in it clears them")
    restricted: bool = Field(description="a project of its agents is restricted: its notices name it and link only")
    last_seq: int = Field(description="the number of its last event")
    last_read_seq: int = Field(description="how far the reader read")
    budget_usd: float = Field(
        description="what the runs of its agents may cost in 24 hours, as their usage says; its creator changes it"
    )


class ChatSummary(BaseModel):
    id: int
    title: str
    created_by: str
    created_at: datetime
    last_message_at: datetime | None
    last_seq: int
    unread: int = Field(description="messages of others after the reader's last read")
    people: list[str]
    agents: list[str]


class ChatPage(BaseModel):
    chat: Chat
    messages: list[GroupMessage] = Field(description="oldest first; those the reader's grants do not reach left out")
    more: bool = Field(description="there are older messages: ask again with before= the first seq")


class ChatReactionChange(BaseModel):
    message_id: int
    emoji: str
    by: ChatAuthor
    count: int = Field(description="how many put the emoji on the message after the change")


class ChatMemberChange(BaseModel):
    member: ChatAuthor = Field(description="the person or agent added or removed")
    by: str | None = Field(description="the person who added or removed them")


class ChatEvent(BaseModel):
    seq: int
    kind: Literal[chats.EVENT_KINDS]
    created_at: datetime
    message: GroupMessage | None = Field(None, description="a message event's message, as it reads now")
    reaction: ChatReactionChange | None = None
    member: ChatMemberChange | None = None


class ChatEvents(BaseModel):
    events: list[ChatEvent]
    last_seq: int
    more: bool


# Authors and members


def agent_author(profile: Profile) -> ChatAuthor:
    return ChatAuthor(
        kind="agent",
        handle=profile.handle,
        project=profile.project,
        name=profile.name,
        color=profile.color,
        icon_url=icon_url(profile),
        deleted=profile.deleted_at is not None,
    )


def person_author(login: str) -> ChatAuthor:
    return ChatAuthor(kind="person", login=login, name=login)


HUB_AUTHOR = ChatAuthor(kind="hub", name=HUB_NAME)


def agent_member(profile: Profile, added_by: str | None, added_at: datetime) -> ChatAgent:
    return ChatAgent(
        handle=profile.handle,
        project=profile.project,
        name=profile.name,
        color=profile.color,
        icon_url=icon_url(profile),
        is_default=profile.is_default,
        deleted=profile.deleted_at is not None,
        added_by=added_by,
        added_at=added_at,
    )


async def profiles_by_id(conn: AsyncConnection, ids) -> dict[int, Profile]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await conn.execute(profile_select().where(one_of(tables.agent_profiles.c.id, wanted)))
    return {row.id: profile_of(row) for row in rows}


def _members_query(chat_id: int):
    m, u, adder = tables.chat_members, tables.users, tables.users.alias("adder")
    return (
        select(m.c.user_id, m.c.profile_id, u.c.login, adder.c.login.label("added_by"), m.c.added_at)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(adder, adder.c.id == m.c.added_by))
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
        .order_by(m.c.added_at, m.c.id)
    )


async def chat_view(conn: AsyncConnection, chat: ChatAccess) -> Chat:
    """The chat ``chat`` names, as its reader sees it."""
    rows = (await conn.execute(_members_query(chat.chat_id))).all()
    found = await profiles_by_id(conn, [row.profile_id for row in rows if row.profile_id is not None])
    people = [ChatPerson(login=r.login, added_by=r.added_by, added_at=r.added_at) for r in rows if r.user_id]
    agents = [agent_member(found[r.profile_id], r.added_by, r.added_at) for r in rows if r.profile_id]
    c = tables.chats
    seq, budget = (await conn.execute(select(c.c.event_seq, c.c.budget_usd).where(c.c.id == chat.chat_id))).one()
    m = tables.chat_members
    read = (await conn.execute(select(m.c.last_read_seq).where(m.c.id == chat.member_id))).scalar_one()
    return Chat(
        id=chat.chat_id,
        title=chat.title,
        created_by=chat.creator,
        created_at=chat.created_at,
        people=people,
        agents=agents,
        label=chat.label,
        restricted=chat.restricted,
        last_seq=seq,
        last_read_seq=read,
        budget_usd=budget,
    )


# Messages


def message_select():
    cm, u = tables.chat_messages, tables.users
    return select(
        cm.c.id,
        cm.c.chat_id,
        cm.c.seq,
        cm.c.thread_root_id,
        cm.c.author_user_id,
        cm.c.author_profile_id,
        u.c.login.label("author_login"),
        cm.c.run_id,
        cm.c.body,
        cm.c.label,
        cm.c.created_at,
    ).select_from(cm.outerjoin(u, u.c.id == cm.c.author_user_id))


async def _mentions(conn: AsyncConnection, ids: list[int]) -> dict[int, list[ChatMention]]:
    cn, u, ap = tables.chat_mentions, tables.users, tables.agent_profiles
    query = (
        select(cn.c.message_id, cn.c.name, cn.c.via, cn.c.refusal, cn.c.run_id, u.c.login, ap.c.handle)
        .select_from(cn.outerjoin(u, u.c.id == cn.c.user_id).outerjoin(ap, ap.c.id == cn.c.profile_id))
        .where(one_of(cn.c.message_id, ids))
        .order_by(cn.c.message_id, cn.c.ordinal)
    )
    found: dict[int, list[ChatMention]] = defaultdict(list)
    for row in await conn.execute(query):
        mention = ChatMention(
            name=row.name,
            via=row.via,
            login=row.login,
            handle=row.handle or None,
            refusal=row.refusal,
            run_id=row.run_id,
        )
        found[row.message_id].append(mention)
    return found


async def _reactions(conn: AsyncConnection, ids: list[int], reader_id: int) -> dict[int, list[ChatReaction]]:
    cr, u, ap = tables.chat_reactions, tables.users, tables.agent_profiles
    query = (
        select(cr.c.message_id, cr.c.emoji, cr.c.user_id, u.c.login, ap.c.handle)
        .select_from(cr.outerjoin(u, u.c.id == cr.c.user_id).outerjoin(ap, ap.c.id == cr.c.profile_id))
        .where(one_of(cr.c.message_id, ids))
        .order_by(cr.c.message_id, cr.c.id)
    )
    grouped: dict[int, dict[str, ChatReaction]] = defaultdict(dict)
    for row in await conn.execute(query):
        emoji = grouped[row.message_id].setdefault(
            row.emoji, ChatReaction(emoji=row.emoji, count=0, mine=False, people=[], agents=[])
        )
        emoji.count += 1
        if row.login is not None:
            emoji.people.append(row.login)
            emoji.mine = emoji.mine or row.user_id == reader_id
        elif row.handle is not None:
            emoji.agents.append(row.handle)
    return {message_id: list(emojis.values()) for message_id, emojis in grouped.items()}


async def _reply_counts(conn: AsyncConnection, ids: list[int]) -> dict[int, int]:
    cm = tables.chat_messages
    query = (
        select(cm.c.thread_root_id, func.count()).where(one_of(cm.c.thread_root_id, ids)).group_by(cm.c.thread_root_id)
    )
    return {root: count for root, count in await conn.execute(query)}


def _author(row, profiles: dict[int, Profile]) -> ChatAuthor:
    if row.author_profile_id is not None:
        return agent_author(profiles[row.author_profile_id])
    if row.author_login is not None:
        return person_author(row.author_login)
    return HUB_AUTHOR


async def messages_view(conn: AsyncConnection, rows, reader: LabelReader) -> list[GroupMessage]:
    """The messages of ``rows`` (``message_select``) that ``reader`` clears the label of, in the order given."""
    shown = [row for row in rows if reader.sees(row.label)]
    ids = [row.id for row in shown]
    if not ids:
        return []
    profiles = await profiles_by_id(conn, [row.author_profile_id for row in shown if row.author_profile_id])
    mentions = await _mentions(conn, ids)
    reactions = await _reactions(conn, ids, reader.user_id)
    replies = await _reply_counts(conn, ids)
    return [
        GroupMessage(
            id=row.id,
            chat_id=row.chat_id,
            seq=row.seq,
            thread_root_id=row.thread_root_id,
            reply_count=replies.get(row.id, 0),
            author=_author(row, profiles),
            run_id=row.run_id,
            body=row.body,
            mentions=mentions.get(row.id, []),
            reactions=reactions.get(row.id, []),
            label=row.label,
            created_at=row.created_at,
        )
        for row in shown
    ]


async def message_rows(conn: AsyncConnection, chat_id: int, ids: list[int]) -> list:
    """The rows (``message_select``) of the messages ``ids`` of chat ``chat_id``, oldest first."""
    if not ids:
        return []
    cm = tables.chat_messages
    query = message_select().where(cm.c.chat_id == chat_id, one_of(cm.c.id, ids)).order_by(cm.c.seq)
    return (await conn.execute(query)).all()


# The list of chats


def summaries_query(user_id: int):
    """The chats person ``user_id`` is in, the latest active first, with what ``ChatSummary`` shows."""
    c, m, u, cm = tables.chats, tables.chat_members, tables.users, tables.chat_messages
    mine = m.alias("mine")
    last = select(func.max(cm.c.created_at)).where(cm.c.chat_id == c.c.id).scalar_subquery()
    unread = (
        select(func.count())
        .where(
            cm.c.chat_id == c.c.id,
            cm.c.seq > mine.c.last_read_seq,
            cm.c.author_user_id.is_distinct_from(user_id),
        )
        .scalar_subquery()
    )
    return (
        select(
            c.c.id,
            c.c.title,
            u.c.login.label("created_by"),
            c.c.created_at,
            last.label("last_message_at"),
            c.c.event_seq.label("last_seq"),
            unread.label("unread"),
        )
        .select_from(c.join(mine, mine.c.chat_id == c.c.id).join(u, u.c.id == c.c.created_by))
        .where(mine.c.user_id == user_id, mine.c.removed_at.is_(None))
        .order_by(func.coalesce(last, c.c.created_at).desc(), c.c.id.desc())
    )


async def member_names(conn: AsyncConnection, chat_ids: list[int]) -> dict[int, tuple[list[str], list[str]]]:
    """(people's logins, agents' handles) of each chat of ``chat_ids``, in the order they were added."""
    if not chat_ids:
        return {}
    m, u, ap = tables.chat_members, tables.users, tables.agent_profiles
    query = (
        select(m.c.chat_id, u.c.login, ap.c.handle)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(ap, ap.c.id == m.c.profile_id))
        .where(one_of(m.c.chat_id, chat_ids), m.c.removed_at.is_(None))
        .order_by(m.c.chat_id, m.c.added_at, m.c.id)
    )
    found: dict[int, tuple[list[str], list[str]]] = {chat_id: ([], []) for chat_id in chat_ids}
    for row in await conn.execute(query):
        people, agents = found[row.chat_id]
        if row.login is not None:
            people.append(row.login)
        else:
            agents.append(row.handle)
    return found
