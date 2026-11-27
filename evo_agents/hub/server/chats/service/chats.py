"""Group chats (``evo_agents.hub.chats``): what is behind GET and POST /v1/chats, GET /v1/chats/{id}, GET and POST
.../messages, POST and DELETE .../messages/{message}/reactions and POST .../read. ``chats.service.messages`` reads a
message's mentions and hands the work, ``chats.service.members`` adds and removes people and agents,
``chats.service.images`` sets a group chat's image, ``chats.service.events`` streams what happens,
``chats.service.summaries`` puts the list together, and ``docs/hub.md`` (Group chats) is the design.

A signed-in person creates a chat with a title and the people and agents it starts with, under the checks of
``chats.service.members``; they are its creator and first member. Only members read a chat and write to it
(``chats.service.access``), and each reads the messages whose label they clear. GET .../messages answers the chat with
a page of its messages, the latest ``limit`` before ``before`` (a seq), oldest first, or those of one thread
(``thread``, its first message's id).

POST .../messages sends a message of at most ``chats.MAX_MESSAGE_BYTES``; ``reply_to`` puts it in the thread of the
message it answers, and ``from_answer`` in the thread of the interactive answer it is sent from, to the agent that
wrote the answer (``chats.service.answers``). A message these routes answer carries how far each run it handed work to
is while that run is on its way (``chats.service.progress``): waiting for its worker, a step of its setup, working, as
the reader may read the run.

A member puts an emoji on a message and takes it off; a message carries at most ``chats.MAX_EMOJI_KINDS`` different
ones. POST .../read says how far the member read, which the list's unread count starts from. Creating a chat and
sending a message are audited (chat.create, chat.message), never with a title or a text.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.messages import ChatCreate, ChatMessageIn, ChatReactionIn, ChatRead, ChatReadIn
from evo_agents.hub.server.chats.models.views import Chat, ChatPage, GroupMessage, person_author
from evo_agents.hub.server.chats.repository import messages as store
from evo_agents.hub.server.chats.repository.access import projects_by_id
from evo_agents.hub.server.chats.repository.agents import insert_chat
from evo_agents.hub.server.chats.repository.store import (
    add_member,
    emoji_kinds,
    mark_read,
    member_event,
    put_reaction,
    take_reaction,
)
from evo_agents.hub.server.chats.repository.views import message_rows
from evo_agents.hub.server.chats.service.access import chat_access, label_reader, uncleared
from evo_agents.hub.server.chats.service.members import (
    addable_agent,
    label_now,
    person,
    put_agent,
    put_person,
    refuse_clash,
    refuse_uncleared,
)
from evo_agents.hub.server.chats.service.messages import deliver_message, message_view, visible_message
from evo_agents.hub.server.chats.service.progress import by_message, chat_progress
from evo_agents.hub.server.chats.service.views import chat_view, messages_view
from evo_agents.hub.server.security import Principal

# The chats


async def _starting(conn: AsyncConnection, user: Principal, body: ChatCreate):
    """(agents, {person id: login}) a new chat starts with, once the checks of ``chats.service.members`` pass."""
    agents = [await addable_agent(conn, user, handle) for handle in dict.fromkeys(body.agents)]
    people = dict([await person(conn, login) for login in body.people if login.lower() != user.login.lower()])
    refuse_clash([user.login, *people.values()], [agent.handle for agent in agents])
    projects = await projects_by_id(conn, [agent.project_id for agent in agents])
    # the creator clears every part, so as a writer on each agent's project they share it with the people they name
    refuse_uncleared(await uncleared(conn, {user.user_id: user.login}, projects), "the chat")
    return agents, people


async def create_chat(engine: AsyncEngine, user: Principal, body: ChatCreate) -> tuple[Chat, int, int, int]:
    """Create a chat with ``user`` in it, and the people and agents given: (the chat, its id, how many people and
    agents it starts with)."""
    async with UnitOfWork(engine) as conn:
        agents, people = await _starting(conn, user, body)
        chat_id = await insert_chat(conn, body.title, user.user_id)
        await add_member(conn, chat_id, user_id=user.user_id, added_by=user.user_id)
        await member_event(conn, chat_id, "member_added", person_author(user.login), user.login)
        chat = await chat_access(conn, user, chat_id)
        for agent in agents:
            await put_agent(conn, user, chat, agent, share=True)
            chat = await label_now(conn, chat)
        for user_id, login in people.items():
            await put_person(conn, user, chat, user_id, login, share=True)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_CREATE, target=f"chat:{chat_id}"
        )
        return await chat_view(conn, chat), chat_id, len(people) + 1, len(agents)


async def show_chat(engine: AsyncEngine, user: Principal, chat_id: int) -> Chat:
    """The chat with its people and agents."""
    async with UnitOfWork(engine) as conn:
        return await chat_view(conn, await chat_access(conn, user, chat_id))


# Messages


async def list_messages(
    engine: AsyncEngine, user: Principal, chat_id: int, before: int | None, thread: int | None, limit: int
) -> ChatPage:
    """The chat and the latest ``limit`` messages before ``before``, or of one thread, oldest first."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        rows = await store.page_rows(conn, chat_id, before, thread, limit)
        page = list(reversed(rows[:limit]))
        reader = await label_reader(conn, user.user_id, [row.label for row in page])
        progress = by_message(await chat_progress(conn, user, chat_id)) if page else None
        messages = await messages_view(conn, page, reader, progress)
        view = await chat_view(conn, chat)
        return ChatPage(chat=view, messages=messages, more=len(rows) > limit)


async def send_message(engine: AsyncEngine, user: Principal, chat_id: int, body: ChatMessageIn) -> GroupMessage:
    """Send a message to the chat, in the thread of ``reply_to`` when given."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        message_id = await deliver_message(conn, chat, user, body)
        return await message_view(conn, chat, (await message_rows(conn, chat_id, [message_id]))[0], user)


# Reactions


async def react(
    engine: AsyncEngine, user: Principal, chat_id: int, message_id: int, body: ChatReactionIn
) -> GroupMessage:
    """Put an emoji on a message; nothing changes when ``user`` put it already."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        row = await visible_message(conn, chat, message_id)
        kinds = await emoji_kinds(conn, message_id)
        if body.emoji not in kinds and len(kinds) >= chats.MAX_EMOJI_KINDS:
            raise HTTPException(
                409,
                f"message {message_id} carries {len(kinds)} different emoji, the most one holds: add to one of them",
            )
        await put_reaction(conn, chat_id, message_id, body.emoji, person_author(user.login), user_id=user.user_id)
        return await message_view(conn, chat, row, user)


async def unreact(engine: AsyncEngine, user: Principal, chat_id: int, message_id: int, emoji: str) -> GroupMessage:
    """Take ``user``'s emoji off a message; nothing changes when it is not there."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        row = await visible_message(conn, chat, message_id)
        await take_reaction(conn, chat_id, message_id, emoji, person_author(user.login), user_id=user.user_id)
        return await message_view(conn, chat, row, user)


# Reading


async def read_chat(engine: AsyncEngine, user: Principal, chat_id: int, body: ChatReadIn) -> ChatRead:
    """Say how far ``user`` read: the seq of the last event they saw, at most the chat's last."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        last = await mark_read(conn, chat.member_id, min(body.seq, chat.event_seq))
        return ChatRead(last_read_seq=last, unread=await store.unread(conn, chat_id, last, user.user_id))
