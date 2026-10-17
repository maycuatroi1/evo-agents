"""What changes a group chat (``evo_agents.hub.chats``) in the database: its members, its messages with their mentions,
the reactions on them, the events its stream sends and the notices it sends, each in the caller's transaction.

Every change writes one event of the chat (chat_events), numbered by the chat (``chats.event_seq``, counted under its
row lock, so a reader never sees a number before the ones below it), and notifies CHANNEL with the chat's id once the
transaction commits, which wakes the chat's streams (``chat_events.ChatStreams``) on the api process's one LISTEN. A
message is numbered by the event that brought it.

``status_reaction`` is how a run of an agent marks the message that started it as the run moves (``chats.
STATUS_REACTIONS``): by rule, with no model. A run calls it in the transaction that moves it, so this module imports
neither the notifications nor the runs (``chat_notices`` sends the notices).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, plugin_events, tables
from evo_agents.hub.server.chat_access import ChatAccess
from evo_agents.hub.server.chat_views import ChatAuthor, agent_author, profiles_by_id

CHANNEL = "evo_chat_events"  # notified with a chat's id when an event of it is written, so its streams look again


# Events


async def next_seq(conn: AsyncConnection, chat_id: int) -> int:
    """The chat's next event number, its row locked until the caller's transaction ends."""
    c = tables.chats
    statement = update(c).values(event_seq=c.c.event_seq + 1).where(c.c.id == chat_id).returning(c.c.event_seq)
    return (await conn.execute(statement)).scalar_one()


async def write_event(conn: AsyncConnection, chat_id: int, kind: str, body: dict, seq: int | None = None) -> int:
    """Write an event of chat ``chat_id`` (numbered ``seq``, or the next number) and wake its streams; its seq."""
    if seq is None:
        seq = await next_seq(conn, chat_id)
    await conn.execute(insert(tables.chat_events).values(chat_id=chat_id, seq=seq, kind=kind, body=body))
    await conn.execute(select(func.pg_notify(CHANNEL, str(chat_id))))
    return seq


# Members


async def add_member(
    conn: AsyncConnection,
    chat_id: int,
    *,
    user_id: int | None = None,
    profile_id: int | None = None,
    added_by: int | None,
    added_by_run_id: int | None = None,
) -> datetime | None:
    """Make a person or an agent a member of the chat: when they were added, None when they were in it already.
    Someone who left is a member again, added now."""
    m = tables.chat_members
    key = m.c.user_id if user_id is not None else m.c.profile_id
    values = {"added_by": added_by, "added_by_run_id": added_by_run_id, "added_at": func.now(), "removed_at": None}
    statement = (
        pg_insert(m)
        .values(chat_id=chat_id, user_id=user_id, profile_id=profile_id, **values)
        .on_conflict_do_update(index_elements=[m.c.chat_id, key], set_=values, where=m.c.removed_at.is_not(None))
        .returning(m.c.added_at)
    )
    return (await conn.execute(statement)).scalar_one_or_none()


async def remove_member(conn: AsyncConnection, chat_id: int, *, user_id=None, profile_id=None) -> bool:
    """Take a person or an agent out of the chat; whether they were in it."""
    m = tables.chat_members
    key = m.c.user_id == user_id if user_id is not None else m.c.profile_id == profile_id
    statement = (
        update(m)
        .values(removed_at=func.now())
        .where(m.c.chat_id == chat_id, key, m.c.removed_at.is_(None))
        .returning(m.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none() is not None


async def member_event(conn: AsyncConnection, chat_id: int, kind: str, member: ChatAuthor, by: str | None) -> int:
    """The event of a person or an agent added to the chat or removed from it (member_added, member_removed)."""
    return await write_event(conn, chat_id, kind, {"member": member.model_dump(mode="json"), "by": by})


async def mark_read(conn: AsyncConnection, member_id: int, seq: int) -> int:
    """Move how far a member read to ``seq`` when it is further; how far they read now."""
    m = tables.chat_members
    statement = (
        update(m)
        .values(last_read_seq=func.greatest(m.c.last_read_seq, seq))
        .where(m.c.id == member_id)
        .returning(m.c.last_read_seq)
    )
    return (await conn.execute(statement)).scalar_one()


# Messages


async def insert_message(
    conn: AsyncConnection,
    chat: ChatAccess,
    body: str,
    *,
    thread_root_id: int | None,
    author_user_id: int | None = None,
    author_profile_id: int | None = None,
    run_id: int | None = None,
) -> tuple[int, int]:
    """Store a message of a person, an agent or the hub (neither) in the chat, with the chat's label now, and its
    event; (its id, its seq)."""
    seq = await next_seq(conn, chat.chat_id)
    cm = tables.chat_messages
    statement = (
        insert(cm)
        .values(
            chat_id=chat.chat_id,
            seq=seq,
            thread_root_id=thread_root_id,
            author_user_id=author_user_id,
            author_profile_id=author_profile_id,
            run_id=run_id,
            body=body,
            label=chat.label,
        )
        .returning(cm.c.id)
    )
    message_id = (await conn.execute(statement)).scalar_one()
    await write_event(conn, chat.chat_id, "message", {"message_id": message_id}, seq)
    by = "person" if author_user_id is not None else "agent" if author_profile_id is not None else "hub"
    said = {"chat_id": chat.chat_id, "message_id": message_id, "seq": seq, "thread_root_id": thread_root_id, "by": by}
    for project in chat.projects:  # the projects of its agents: a chat of people alone belongs to none
        await plugin_events.emit(conn, "chat.message", project.id, {**said, "run_id": run_id})
    return message_id, seq


async def insert_mentions(conn: AsyncConnection, message_id: int, mentions: list[dict]) -> None:
    """Keep what message ``message_id`` mentions: dicts of name, via, user_id, profile_id, refusal and the run it made
    (run_id, none when left out), in order."""
    if mentions:
        rows = [{"message_id": message_id, "ordinal": n, "run_id": None, **m} for n, m in enumerate(mentions)]
        await conn.execute(insert(tables.chat_mentions), rows)


# Reactions


def _reactor(user_id: int | None, profile_id: int | None):
    r = tables.chat_reactions
    return r.c.user_id == user_id if user_id is not None else r.c.profile_id == profile_id


async def emoji_count(conn: AsyncConnection, message_id: int, emoji: str) -> int:
    r = tables.chat_reactions
    query = select(func.count()).where(r.c.message_id == message_id, r.c.emoji == emoji)
    return (await conn.execute(query)).scalar_one()


async def emoji_kinds(conn: AsyncConnection, message_id: int) -> set[str]:
    r = tables.chat_reactions
    return set((await conn.execute(select(r.c.emoji).where(r.c.message_id == message_id).distinct())).scalars())


async def put_reaction(
    conn: AsyncConnection,
    chat_id: int,
    message_id: int,
    emoji: str,
    by: ChatAuthor,
    *,
    user_id: int | None = None,
    profile_id: int | None = None,
) -> bool:
    """Put ``emoji`` of a person or an agent on a message, with its event; whether it was not there already."""
    r = tables.chat_reactions
    key = r.c.user_id if user_id is not None else r.c.profile_id
    statement = (
        pg_insert(r)
        .values(message_id=message_id, user_id=user_id, profile_id=profile_id, emoji=emoji)
        .on_conflict_do_nothing(index_elements=[r.c.message_id, key, r.c.emoji])
        .returning(r.c.id)
    )
    if (await conn.execute(statement)).scalar_one_or_none() is None:
        return False
    await _reaction_event(conn, chat_id, "reaction_added", message_id, emoji, by)
    return True


async def take_reaction(
    conn: AsyncConnection,
    chat_id: int,
    message_id: int,
    emoji: str,
    by: ChatAuthor,
    *,
    user_id: int | None = None,
    profile_id: int | None = None,
) -> bool:
    """Take ``emoji`` of a person or an agent off a message, with its event; whether it was there."""
    r = tables.chat_reactions
    statement = (
        delete(r)
        .where(r.c.message_id == message_id, r.c.emoji == emoji, _reactor(user_id, profile_id))
        .returning(r.c.id)
    )
    if (await conn.execute(statement)).scalar_one_or_none() is None:
        return False
    await _reaction_event(conn, chat_id, "reaction_removed", message_id, emoji, by)
    return True


async def _reaction_event(conn, chat_id: int, kind: str, message_id: int, emoji: str, by: ChatAuthor) -> None:
    count = await emoji_count(conn, message_id, emoji)
    body = {"message_id": message_id, "emoji": emoji, "by": by.model_dump(mode="json"), "count": count}
    await write_event(conn, chat_id, kind, body)


async def status_reaction(conn: AsyncConnection, *, message_id: int, profile_id: int, state: str) -> str | None:
    """Mark message ``message_id`` with the reaction of agent ``profile_id`` for a run of it that moved to ``state``,
    by rule (``chats.status_reaction``): the eyes when a worker claimed it (leased), a check mark when it is done, a
    cross when it failed or was cancelled, the check mark and the cross taking the eyes off. The emoji put, or None
    when the state puts none or it was there already. No model is asked anything."""
    found = chats.status_reaction(state)
    if found is None:
        return None
    emoji, taken_off = found
    cm = tables.chat_messages
    chat_id = (await conn.execute(select(cm.c.chat_id).where(cm.c.id == message_id))).scalar_one()
    by = agent_author((await profiles_by_id(conn, [profile_id]))[profile_id])
    for old in taken_off:
        await take_reaction(conn, chat_id, message_id, old, by, profile_id=profile_id)
    put = await put_reaction(conn, chat_id, message_id, emoji, by, profile_id=profile_id)
    return emoji if put else None
