"""A person's message sent from an interactive answer (``chats.service.messages``, ``outputs.check_interactive``): POST
/v1/chats/{id}/messages with ``from_answer``, the answer's output id, which the web's frame of the answer sends when a
button in it asks a question (``docs/hub.md``, Interactive answers).

``answer_of`` finds the answer: an interactive output that a message of this chat carries and the sender reads (its
label cleared), else 404 as if there were none. The message goes in that message's thread, to the agent whose profile
wrote it, as a mention ``via`` ``chats.ANSWER`` (``answer_mention``), which ``chats.service.runs.hand_mentions`` hands
as any mention: the next turn of the sender's session with that agent in the thread, by the agent's profile wherever its
runs go. An agent no longer in the chat, or deleted, gets the refusal a mention of it gets. ``refuse_too_many`` holds a
person to ``chats.ANSWER_PROMPTS`` such messages in ``chats.ANSWER_WINDOW`` seconds (429).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.answers import DELETED, NO_ANSWER, NOT_IN_CHAT, OTHER_THREAD, TOO_MANY, Answer
from evo_agents.hub.server.chats.repository import answers as store
from evo_agents.hub.server.chats.service.access import label_reader


async def answer_of(conn: AsyncConnection, chat: ChatAccess, output_id: int) -> Answer:
    """The interactive answer ``output_id`` as the earliest message of ``chat`` that carries it and its reader reads;
    404 when there is none."""
    rows = await store.answer_rows(conn, chat.chat_id, output_id)
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
    row = await store.answer_agent(conn, chat.chat_id, answer.profile_id)
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
    count = await store.sent_lately(conn, user_id)
    if count >= chats.ANSWER_PROMPTS:
        raise HTTPException(
            429,
            TOO_MANY.format(count=count, window=chats.ANSWER_WINDOW),
            headers={"Retry-After": str(chats.ANSWER_WINDOW)},
        )
