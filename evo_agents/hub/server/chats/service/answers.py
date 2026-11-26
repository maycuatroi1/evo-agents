"""A person's message sent from an interactive answer (``chats``, ``outputs.check_interactive``): POST
/v1/chats/{id}/messages with ``from_answer``, the answer's output id, which the web's frame of the answer sends when a
button in it asks a question (``docs/hub.md``, Interactive answers).

``answer_of`` finds the answer: an interactive output that a message of this chat carries and the sender reads (its
label cleared), else 404 as if there were none. The message goes in that message's thread, to the agent whose profile
wrote it, as a mention ``via`` ``chats.ANSWER`` (``answer_mention``), which ``chat_runs.hand_mentions`` hands as any
mention: the next turn of the sender's session with that agent in the thread, by the agent's profile wherever its runs
go. An agent no longer in the chat, or deleted, gets the refusal a mention of it gets. ``refuse_too_many`` holds a
person to ``chats.ANSWER_PROMPTS`` such messages in ``chats.ANSWER_WINDOW`` seconds (429).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server.chat_access import ChatAccess, label_reader

NO_ANSWER = "chat {chat} has no interactive answer {id} that you can read"
OTHER_THREAD = "message {reply_to} is not in the thread of interactive answer {id}: send it without reply_to"
TOO_MANY = (
    "you sent {count} messages from interactive answers in the last {window} seconds, the most a person sends: "
    "wait a moment, or write in the chat"
)
NOT_IN_CHAT = "@{name} is not in this chat any more: add it to the chat, then ask again"
DELETED = "@{name} was deleted: it takes no new work"


@dataclass(frozen=True)
class Answer:
    """The interactive answer ``output_id``, carried by message ``message_id`` that agent ``profile_id`` wrote, in the
    thread of ``root_id``."""

    output_id: int
    message_id: int
    root_id: int
    profile_id: int


async def answer_of(conn: AsyncConnection, chat: ChatAccess, output_id: int) -> Answer:
    """The interactive answer ``output_id`` as the earliest message of ``chat`` that carries it and its reader reads;
    404 when there is none."""
    k, cm, o = tables.run_output_links, tables.chat_messages, tables.run_outputs
    query = (
        select(cm.c.id, cm.c.thread_root_id, cm.c.author_profile_id, cm.c.label)
        .join_from(k, cm, cm.c.id == k.c.chat_message_id)
        .join(o, o.c.id == k.c.output_id)
        .where(
            k.c.output_id == output_id,
            cm.c.chat_id == chat.chat_id,
            cm.c.author_profile_id.is_not(None),
            o.c.interactive.is_(True),
        )
        .order_by(cm.c.id)
    )
    rows = (await conn.execute(query)).all()
    reader = await label_reader(conn, chat.user_id, [row.label for row in rows])
    for row in rows:
        if reader.sees(row.label):
            return Answer(output_id, row.id, row.thread_root_id or row.id, row.author_profile_id)
    raise HTTPException(404, NO_ANSWER.format(chat=chat.chat_id, id=output_id))


def thread_of(answer: Answer, reply_root: int | None, reply_to: int | None) -> int:
    """The thread a message from ``answer`` goes in: the answer's; 422 when ``reply_to`` (whose thread starts at
    ``reply_root``) names another."""
    if reply_root is not None and reply_root != answer.root_id:
        raise HTTPException(422, OTHER_THREAD.format(reply_to=reply_to, id=answer.output_id))
    return answer.root_id


async def answer_mention(conn: AsyncConnection, chat: ChatAccess, answer: Answer) -> dict:
    """The mention, ``via`` ANSWER, that hands a message from ``answer`` to the agent that wrote it, with the refusal
    of an agent no longer in the chat, or deleted."""
    ap, m = tables.agent_profiles, tables.chat_members
    member = (
        select(m.c.id)
        .where(m.c.chat_id == chat.chat_id, m.c.profile_id == ap.c.id, m.c.removed_at.is_(None))
        .exists()
        .label("member")
    )
    row = (await conn.execute(select(ap.c.handle, ap.c.deleted_at, member).where(ap.c.id == answer.profile_id))).one()
    refusal = None
    if row.deleted_at is not None:
        refusal = DELETED.format(name=row.handle)
    elif not row.member:
        refusal = NOT_IN_CHAT.format(name=row.handle)
    return {
        "name": row.handle,
        "via": chats.ANSWER,
        "user_id": None,
        "profile_id": answer.profile_id,
        "refusal": refusal,
    }


async def refuse_too_many(conn: AsyncConnection, user_id: int) -> None:
    """429 when person ``user_id`` sent ANSWER_PROMPTS messages from interactive answers in the last ANSWER_WINDOW
    seconds."""
    cm = tables.chat_messages
    since = func.now() - timedelta(seconds=chats.ANSWER_WINDOW)
    query = select(func.count()).where(
        cm.c.author_user_id == user_id, cm.c.from_output_id.is_not(None), cm.c.created_at > since
    )
    count = (await conn.execute(query)).scalar_one()
    if count >= chats.ANSWER_PROMPTS:
        raise HTTPException(
            429,
            TOO_MANY.format(count=count, window=chats.ANSWER_WINDOW),
            headers={"Retry-After": str(chats.ANSWER_WINDOW)},
        )
