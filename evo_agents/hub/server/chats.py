"""Group chats (``evo_agents.hub.chats``): GET and POST /v1/chats, GET /v1/chats/{id}, GET and POST
.../messages, POST and DELETE .../messages/{message}/reactions and POST .../read. ``chat_members`` adds and removes
people and agents, ``chat_events`` streams what happens, and ``docs/hub.md`` (Group chats) is the design.

A signed-in person creates a chat with a title and the people and agents it starts with, under the checks of
``chat_members``; they are its creator and first member. Only members read a chat and write to it (``chat_access``),
and each reads the messages whose label they clear. GET .../messages answers the chat with a page of its messages,
the latest ``limit`` before ``before`` (a seq), oldest first, or those of one thread (``thread``, its first message's
id).

POST .../messages sends a message of at most ``chats.MAX_MESSAGE_BYTES``; ``reply_to`` puts it in the thread of the
message it answers. The hub reads its mentions once (``chats.mention_names``) and keeps them with it: a name resolves
among the chat's members only, a person by login, an agent by handle, a project's name to its default agent. A person
mentioned gets the notice chat_mention. An agent mentioned that is not in the chat, or was deleted, reaches no one:
the mention keeps why, and the hub answers in the message's thread, naming it. An agent mentioned gets the work: a
chat run for the sender (``chat_runs.hand_mentions``, at most ``chat_runs.MAX_RUNS_PER_MESSAGE`` agents a message),
and the mention keeps the run it made or woke, or why it made none, which the hub's answer names too. In a chat of one
person, a message in a thread without a mention goes to the one agent whose session the sender has in that thread
(the mention ``via`` thread). A message that mentions no agent makes no run and asks no model anything.

PATCH /v1/chats/{id} changes the chat's budget: what its agents' runs may cost in 24 hours (``chat_runs``); only its
creator does (403), audited as chat.budget.

A member puts an emoji on a message and takes it off; a message carries at most ``chats.MAX_EMOJI_KINDS`` different
ones. POST .../read says how far the member read, which the list's unread count starts from. Creating a chat and
sending a message are audited (chat.create, chat.message), never with a title or a text.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs, chats, profiles, tables
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.db import one_of
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_access import (
    ChatAccess,
    chat_access,
    label_reader,
    projects_by_id,
    uncleared,
)
from evo_agents.hub.server.chat_members import (
    LOGIN_PATTERN,
    REFUSALS,
    ChatId,
    addable_agent,
    label_now,
    person,
    put_agent,
    put_person,
    refuse_clash,
    refuse_uncleared,
)
from evo_agents.hub.server.chat_notices import notice_mention
from evo_agents.hub.server.chat_runs import Hand, hand_mentions, hub_reply, thread_session
from evo_agents.hub.server.chat_store import (
    add_member,
    emoji_kinds,
    insert_mentions,
    insert_message,
    mark_read,
    member_event,
    put_reaction,
    take_reaction,
)
from evo_agents.hub.server.chat_views import (
    Chat,
    ChatPage,
    ChatSummary,
    GroupMessage,
    chat_view,
    member_names,
    message_rows,
    message_select,
    messages_view,
    person_author,
    summaries_query,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import LINE, MAX_ID
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

PAGE = 50  # messages one page holds without limit
MAX_PAGE = 200
MessageId = Annotated[int, Path(ge=1, le=MAX_ID)]
Login = Annotated[str, Field(pattern=LOGIN_PATTERN)]
Handle = Annotated[str, Field(pattern=profiles.HANDLE_PATTERN)]
NOT_THERE = "chat {chat} has no message {id} that you can read"

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


# Models


class ChatCreate(BaseModel):
    title: str = Field(min_length=1, max_length=chats.MAX_TITLE_CHARS, pattern=LINE)
    people: list[Login] = Field(
        default_factory=list, max_length=chats.MAX_PEOPLE - 1, description="logins; the creator is in it anyway"
    )
    agents: list[Handle] = Field(default_factory=list, max_length=chats.MAX_AGENTS, description="handles, @ left out")


class ChatMessageIn(BaseModel):
    body: str = Field(min_length=1, description=f"at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    reply_to: int | None = Field(None, ge=1, le=MAX_ID, description="the message it answers: it goes in its thread")

    @field_validator("body")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a message has some text")
        if len(value.encode()) > chats.MAX_MESSAGE_BYTES:
            raise ValueError(f"a message is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
        return value


class ChatReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=chats.MAX_EMOJI_CHARS)

    @field_validator("emoji")
    @classmethod
    def _one_emoji(cls, value: str) -> str:
        problem = chats.emoji_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class ChatReadIn(BaseModel):
    seq: int = Field(ge=0, le=MAX_ID, description="the seq of the last event the member read")


class ChatRead(BaseModel):
    last_read_seq: int
    unread: int


class ChatUpdate(BaseModel):
    budget_usd: float = Field(
        gt=0,
        le=chat_runs.MAX_BUDGET_USD,
        description=f"what the runs of the chat's agents may cost in {chat_runs.BUDGET_HOURS} hours, in USD",
    )


# The chats


@router.get("", response_model=list[ChatSummary])
async def list_chats(request: Request, user: CurrentUser) -> list[ChatSummary]:
    """The chats the caller is in, the latest active first, with the unread messages of others."""
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(summaries_query(user.user_id))).all()
        names = await member_names(conn, [row.id for row in rows])
    return [ChatSummary(**row._mapping, people=names[row.id][0], agents=names[row.id][1]) for row in rows]


async def _starting(conn: AsyncConnection, user: Principal, body: ChatCreate):
    """(agents, {person id: login}) a new chat starts with, once the checks of ``chat_members`` pass."""
    agents = [await addable_agent(conn, user, handle) for handle in dict.fromkeys(body.agents)]
    people = dict([await person(conn, login) for login in body.people if login.lower() != user.login.lower()])
    refuse_clash([user.login, *people.values()], [agent.handle for agent in agents])
    projects = await projects_by_id(conn, [agent.project_id for agent in agents])
    refuse_uncleared(await uncleared(conn, {user.user_id: user.login, **people}, projects), "the chat")
    return agents, people


@router.post("", status_code=201, response_model=Chat, responses=REFUSALS)
async def create_chat(request: Request, body: ChatCreate, user: CurrentUser) -> Chat:
    """Create a chat with the caller in it, and the people and agents given."""
    async with request.app.state.engine.begin() as conn:
        agents, people = await _starting(conn, user, body)
        c = tables.chats
        statement = insert(c).values(title=body.title, created_by=user.user_id).returning(c.c.id)
        chat_id = (await conn.execute(statement)).scalar_one()
        await add_member(conn, chat_id, user_id=user.user_id, added_by=user.user_id)
        await member_event(conn, chat_id, "member_added", person_author(user.login), user.login)
        chat = await chat_access(conn, user, chat_id)
        for agent in agents:
            await put_agent(conn, user, chat, agent)
            chat = await label_now(conn, chat)
        for user_id, login in people.items():
            await put_person(conn, user, chat, user_id, login)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_CREATE, target=f"chat:{chat_id}"
        )
        view = await chat_view(conn, chat)
    log.info("chat created", extra={"chat_id": chat_id, "people": len(people) + 1, "agents": len(agents)})
    return view


@router.get("/{chat_id}", response_model=Chat, responses=REFUSALS)
async def show_chat(request: Request, chat_id: ChatId, user: CurrentUser) -> Chat:
    """The chat with its people and agents."""
    async with request.app.state.engine.begin() as conn:
        return await chat_view(conn, await chat_access(conn, user, chat_id))


# Messages


def _page(chat_id: int, before: int | None, thread: int | None, limit: int):
    cm = tables.chat_messages
    query = message_select().where(cm.c.chat_id == chat_id)
    if before is not None:
        query = query.where(cm.c.seq < before)
    if thread is not None:
        query = query.where(or_(cm.c.id == thread, cm.c.thread_root_id == thread))
    return query.order_by(cm.c.seq.desc()).limit(limit + 1)


@router.get("/{chat_id}/messages", response_model=ChatPage, responses=REFUSALS)
async def list_messages(
    request: Request,
    chat_id: ChatId,
    user: CurrentUser,
    before: Annotated[int | None, Query(ge=1, le=MAX_ID, description="messages before this seq")] = None,
    thread: Annotated[int | None, Query(ge=1, le=MAX_ID, description="the first message of a thread")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = PAGE,
) -> ChatPage:
    """The chat and the latest ``limit`` messages before ``before``, or of one thread, oldest first."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id)
        rows = (await conn.execute(_page(chat_id, before, thread, limit))).all()
        page = list(reversed(rows[:limit]))
        reader = await label_reader(conn, user.user_id, [row.label for row in page])
        messages = await messages_view(conn, page, reader)
        view = await chat_view(conn, chat)
    return ChatPage(chat=view, messages=messages, more=len(rows) > limit)


async def _visible_message(conn: AsyncConnection, chat: ChatAccess, message_id: int):
    """The row (``message_select``) of message ``message_id`` of the chat, when the reader clears its label; 404
    otherwise."""
    rows = await message_rows(conn, chat.chat_id, [message_id])
    reader = await label_reader(conn, chat.user_id, [row.label for row in rows])
    if not rows or not reader.sees(rows[0].label):
        raise HTTPException(404, NOT_THERE.format(chat=chat.chat_id, id=message_id))
    return rows[0]


async def _members_by_name(conn: AsyncConnection, chat_id: int) -> dict[str, dict]:
    """The members of the chat by the name a mention resolves: a person's login (lowered), an agent's handle."""
    m, u, ap = tables.chat_members, tables.users, tables.agent_profiles
    query = (
        select(m.c.user_id, m.c.profile_id, u.c.login, ap.c.handle, ap.c.is_default, ap.c.deleted_at)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(ap, ap.c.id == m.c.profile_id))
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    )
    found = {}
    for row in await conn.execute(query):
        if row.user_id is not None:
            found[row.login.lower()] = {"user_id": row.user_id, "via": "login"}
        else:
            via = "project" if row.is_default else "handle"
            deleted = row.deleted_at is not None
            found[row.handle] = {"profile_id": row.profile_id, "via": via, "deleted": deleted}
    return found


async def _outsiders(conn: AsyncConnection, user: Principal, names: list[str]) -> dict[str, int]:
    """The agents ``names`` names that ``user`` can see (a grant on their project) but are not in the chat: handle to
    profile id."""
    if not names:
        return {}
    ap, g = tables.agent_profiles, tables.grants
    query = (
        select(ap.c.handle, ap.c.id)
        .join_from(ap, g, (g.c.project_id == ap.c.project_id) & (g.c.user_id == user.user_id))
        .where(one_of(ap.c.handle, names))
    )
    return {row.handle: row.id for row in await conn.execute(query)}


async def resolve_mentions(conn: AsyncConnection, chat: ChatAccess, user: Principal, names: list[str]):
    """(the mentions to keep, the people they reach, why the agents they name reach no one) of ``names``."""
    members = await _members_by_name(conn, chat.chat_id)
    outsiders = await _outsiders(conn, user, [name for name in names if name not in members])
    kept, people, refusals = [], [], []
    for name in names:
        found = members.get(name)
        if found is None and name in outsiders:
            refusal = f"@{name} is not in this chat: add it to the chat, then mention it again"
            kept.append({"name": name, "via": None, "user_id": None, "profile_id": outsiders[name], "refusal": refusal})
            refusals.append(refusal)
        elif found is not None and "user_id" in found:
            kept.append(
                {"name": name, "via": "login", "user_id": found["user_id"], "profile_id": None, "refusal": None}
            )
            people.append(found["user_id"])
        elif found is not None:
            refusal = f"@{name} was deleted: it takes no new work" if found["deleted"] else None
            mention = {"name": name, "via": found["via"], "user_id": None, "profile_id": found["profile_id"]}
            kept.append({**mention, "refusal": refusal})
            refusals += [refusal] if refusal else []
    return kept, people, refusals


async def _thread_agent(conn: AsyncConnection, chat: ChatAccess, user: Principal, root: int | None) -> list[dict]:
    """The mention, ``via`` thread, of the one agent whose session ``user`` has in the thread of ``root``, when the
    chat has no other person; none otherwise."""
    m = tables.chat_members
    people = select(func.count()).where(m.c.chat_id == chat.chat_id, m.c.user_id.is_not(None), m.c.removed_at.is_(None))
    if root is None or (await conn.execute(people)).scalar_one() != 1:
        return []
    profile_id = await thread_session(conn, chat.chat_id, root, user.user_id)
    if profile_id is None:
        return []
    handle = (
        await conn.execute(select(tables.agent_profiles.c.handle).where(tables.agent_profiles.c.id == profile_id))
    ).scalar_one()
    return [{"name": handle, "via": "thread", "user_id": None, "profile_id": profile_id, "refusal": None}]


async def _deliver(conn: AsyncConnection, chat: ChatAccess, user: Principal, body: ChatMessageIn) -> int:
    """Store the message with its mentions, the work they hand the agents, the hub's answer to what they refuse and
    the notices; its id."""
    root = None
    if body.reply_to is not None:
        answered = await _visible_message(conn, chat, body.reply_to)
        root = answered.thread_root_id or answered.id
    names = chats.mention_names(body.body)
    kept, people, _ = await resolve_mentions(conn, chat, user, names)
    message_id, seq = await insert_message(conn, chat, body.body, thread_root_id=root, author_user_id=user.user_id)
    if not any(mention["profile_id"] is not None for mention in kept):
        kept += await _thread_agent(conn, chat, user, root)
    via = dispatch_credential(user.kind)
    hand = Hand(chat, message_id, root or message_id, None, user.user_id, user.login, via, 1, user.login, body.body)
    await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    refusals = [mention["refusal"] for mention in kept if mention["refusal"]]
    if refusals:  # the hub's answer, which the sender has read as they read their own
        await hub_reply(conn, chat.chat_id, root or message_id, "\n".join(refusals))
        seq = (
            await conn.execute(select(tables.chats.c.event_seq).where(tables.chats.c.id == chat.chat_id))
        ).scalar_one()
    await mark_read(conn, chat.member_id, seq)
    for user_id in dict.fromkeys(people):
        if user_id != user.user_id:
            await notice_mention(conn, chat, user_id, message_id, body.body)
    target = f"chat:{chat.chat_id} message:{message_id}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_MESSAGE, target=target)
    return message_id


async def _message_view(conn: AsyncConnection, chat: ChatAccess, row) -> GroupMessage:
    (view,) = await messages_view(conn, [row], await label_reader(conn, chat.user_id, [row.label]))
    return view


@router.post("/{chat_id}/messages", status_code=201, response_model=GroupMessage, responses=REFUSALS)
async def send_message(request: Request, chat_id: ChatId, body: ChatMessageIn, user: CurrentUser) -> GroupMessage:
    """Send a message to the chat, in the thread of ``reply_to`` when given."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id)
        message_id = await _deliver(conn, chat, user, body)
        view = await _message_view(conn, chat, (await message_rows(conn, chat_id, [message_id]))[0])
    log.info("chat message sent", extra={"chat_id": chat_id, "message_id": message_id, "mentions": len(view.mentions)})
    return view


@router.patch("/{chat_id}", response_model=Chat, responses=REFUSALS)
async def update_chat(request: Request, chat_id: ChatId, body: ChatUpdate, user: CurrentUser) -> Chat:
    """Change the chat's budget: what its agents' runs may cost in 24 hours. Only its creator does."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        if user.user_id != chat.created_by:
            raise HTTPException(403, f"only {chat.creator}, who created chat {chat_id}, changes its budget")
        c = tables.chats
        await conn.execute(update(c).values(budget_usd=body.budget_usd).where(c.c.id == chat_id))
        target = f"chat:{chat_id} budget_usd={body.budget_usd:g}"
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_BUDGET, target=target)
        view = await chat_view(conn, chat)
    log.info("chat budget changed", extra={"chat_id": chat_id, "budget_usd": body.budget_usd})
    return view


# Reactions


@router.post("/{chat_id}/messages/{message_id}/reactions", response_model=GroupMessage, responses=REFUSALS)
async def react(
    request: Request, chat_id: ChatId, message_id: MessageId, body: ChatReactionIn, user: CurrentUser
) -> GroupMessage:
    """Put an emoji on a message; nothing changes when the caller put it already."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id)
        row = await _visible_message(conn, chat, message_id)
        kinds = await emoji_kinds(conn, message_id)
        if body.emoji not in kinds and len(kinds) >= chats.MAX_EMOJI_KINDS:
            raise HTTPException(
                409,
                f"message {message_id} carries {len(kinds)} different emoji, the most one holds: add to one of them",
            )
        await put_reaction(conn, chat_id, message_id, body.emoji, person_author(user.login), user_id=user.user_id)
        return await _message_view(conn, chat, row)


@router.delete("/{chat_id}/messages/{message_id}/reactions", response_model=GroupMessage, responses=REFUSALS)
async def unreact(
    request: Request,
    chat_id: ChatId,
    message_id: MessageId,
    user: CurrentUser,
    emoji: Annotated[str, Query(min_length=1, max_length=chats.MAX_EMOJI_CHARS, description="the emoji to take off")],
) -> GroupMessage:
    """Take the caller's emoji off a message; nothing changes when it is not there."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id)
        row = await _visible_message(conn, chat, message_id)
        await take_reaction(conn, chat_id, message_id, emoji, person_author(user.login), user_id=user.user_id)
        return await _message_view(conn, chat, row)


# Reading


@router.post("/{chat_id}/read", response_model=ChatRead, responses=REFUSALS)
async def read_chat(request: Request, chat_id: ChatId, body: ChatReadIn, user: CurrentUser) -> ChatRead:
    """Say how far the caller read: the seq of the last event they saw, at most the chat's last."""
    cm = tables.chat_messages
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id)
        last = await mark_read(conn, chat.member_id, min(body.seq, chat.event_seq))
        query = select(func.count()).where(
            cm.c.chat_id == chat_id, cm.c.seq > last, cm.c.author_user_id.is_distinct_from(user.user_id)
        )
        unread = (await conn.execute(query)).scalar_one()
    return ChatRead(last_read_seq=last, unread=unread)
