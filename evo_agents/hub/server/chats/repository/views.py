"""The queries the chat API's answers are read with (``chats.service.views``): a chat's members, state and bridges,
its messages (``message_select``) with their mentions, reactions, replies and bridges, and the list of a person's
chats (``summaries_query``).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import Text, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.chats.models.views import BridgedTo, ChatBridge, ChatMention, ChatReaction
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select


async def profiles_by_id(conn: AsyncConnection, ids) -> dict[int, Profile]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await conn.execute(profile_select().where(one_of(tables.agent_profiles.c.id, wanted)))
    return {row.id: profile_of(row) for row in rows}


def _members_query(chat_id: int):
    m, u, adder = tables.chat_members, tables.users, tables.users.alias("adder")
    return (
        select(m.c.user_id, m.c.profile_id, u.c.login, u.c.github_id, adder.c.login.label("added_by"), m.c.added_at)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(adder, adder.c.id == m.c.added_by))
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
        .order_by(m.c.added_at, m.c.id)
    )


async def member_rows(conn: AsyncConnection, chat_id: int) -> list:
    """The members of chat ``chat_id`` now, in the order they were added: rows (user_id, profile_id, login,
    github_id, added_by, added_at)."""
    return list((await conn.execute(_members_query(chat_id))).all())


async def chat_state(conn: AsyncConnection, chat_id: int, member_id: int) -> tuple[int, str | None, int]:
    """(the chat's last event seq, its image's sha256, how far member ``member_id`` read)."""
    c, m = tables.chats, tables.chat_members
    seq, image = (await conn.execute(select(c.c.event_seq, c.c.image_sha256).where(c.c.id == chat_id))).one()
    read = (await conn.execute(select(m.c.last_read_seq).where(m.c.id == member_id))).scalar_one()
    return seq, image, read


async def bridges_of(conn: AsyncConnection, chat_id: int) -> list[ChatBridge]:
    """The linked bridges of chat ``chat_id``, oldest first."""
    b, u = tables.chat_bridges, tables.users
    query = (
        select(b.c.id, b.c.kind, b.c.external_id, u.c.login, b.c.linked_at)
        .select_from(b.outerjoin(u, u.c.id == b.c.linked_by))
        .where(b.c.chat_id == chat_id, b.c.unlinked_at.is_(None))
        .order_by(b.c.id)
    )
    return [
        ChatBridge(id=r.id, kind=r.kind, external_id=r.external_id, linked_by=r.login, linked_at=r.linked_at)
        for r in await conn.execute(query)
    ]


async def person_avatars(conn: AsyncConnection, logins) -> dict[str, str | None]:
    """The GitHub avatar of each person ``logins`` names, by login lowered; a login of nobody is left out."""
    wanted = sorted({login.lower() for login in logins})
    if not wanted:
        return {}
    u = tables.users
    query = select(u.c.login, u.c.github_id).where(one_of(func.lower(u.c.login, type_=Text), wanted))
    return {row.login.lower(): chats.github_avatar(row.github_id) for row in await conn.execute(query)}


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
        u.c.github_id.label("author_github_id"),
        cm.c.run_id,
        cm.c.body,
        cm.c.label,
        cm.c.created_at,
        cm.c.via,
        cm.c.from_output_id,
        cm.c.guest_name,
        cm.c.repeats_id,
    ).select_from(cm.outerjoin(u, u.c.id == cm.c.author_user_id))


async def mentions_of(conn: AsyncConnection, ids: list[int]) -> dict[int, list[ChatMention]]:
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


async def reactions_of(conn: AsyncConnection, ids: list[int], reader_id: int) -> dict[int, list[ChatReaction]]:
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


async def bridged_of(conn: AsyncConnection, ids: list[int]) -> dict[int, list[BridgedTo]]:
    bm, b = tables.chat_bridge_messages, tables.chat_bridges
    query = (
        select(bm.c.message_id, b.c.id, b.c.kind)
        .join_from(bm, b, b.c.id == bm.c.bridge_id)
        .where(one_of(bm.c.message_id, ids), bm.c.direction == "out")
        .order_by(bm.c.message_id, b.c.id)
    )
    found: dict[int, list[BridgedTo]] = defaultdict(list)
    for row in await conn.execute(query):
        found[row.message_id].append(BridgedTo(bridge_id=row.id, kind=row.kind))
    return found


async def reply_counts(conn: AsyncConnection, ids: list[int]) -> dict[int, int]:
    cm = tables.chat_messages
    query = (
        select(cm.c.thread_root_id, func.count()).where(one_of(cm.c.thread_root_id, ids)).group_by(cm.c.thread_root_id)
    )
    return {root: count for root, count in await conn.execute(query)}


async def card_traces(conn: AsyncConnection, ids: list[int]) -> dict[int, int]:
    """The trace of the work card each message of ``ids`` that carries one carries (``work_cards``), by message id."""
    t = tables.system_traces
    query = select(t.c.card_message_id, t.c.id).where(one_of(t.c.card_message_id, ids))
    return {message: trace for message, trace in await conn.execute(query)}


async def forwarders(conn: AsyncConnection, rows) -> dict[int, str]:
    """The handle of the agent whose run posted each message of ``rows`` an other agent wrote (a forward), by id."""
    runs = {row.id: row.run_id for row in rows if row.run_id is not None and row.author_profile_id is not None}
    if not runs:
        return {}
    r, p = tables.runs, tables.agent_profiles
    query = (
        select(r.c.id, r.c.profile_id, p.c.handle)
        .join(p, p.c.id == r.c.profile_id)
        .where(one_of(r.c.id, sorted(set(runs.values()))))
    )
    by_run = {row.id: (row.profile_id, row.handle) for row in await conn.execute(query)}
    authors = {row.id: row.author_profile_id for row in rows}
    return {
        message: by_run[run][1] for message, run in runs.items() if run in by_run and by_run[run][0] != authors[message]
    }


async def message_rows(conn: AsyncConnection, chat_id: int, ids: list[int]) -> list:
    """The rows (``message_select``) of the messages ``ids`` of chat ``chat_id``, oldest first."""
    if not ids:
        return []
    cm = tables.chat_messages
    query = message_select().where(cm.c.chat_id == chat_id, one_of(cm.c.id, ids)).order_by(cm.c.seq)
    return list((await conn.execute(query)).all())


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
