"""The agent of a run in group chats (``evo_agents.hub.chat_runs``): what is behind ``evo-agents worker chat
post|react|create|mention``. ``docs/workers.md`` (Chat runs) is the protocol; ``chats.service.pulls`` has the pull
request of a chat run's branch and the repos its agent opens.

Each command takes the worker token of the worker that holds the run (404 for a run it does not hold), of a kind of
``chat_runs.COMMAND_KINDS``: a chat run, or a step, plan or author run. The run's agent is its profile, which must be of
the run's project (409 otherwise: the work of another project does not go into a chat of this one's agent), and it
acts for the person who dispatched the run, the origin of the chain: it writes only in a chat that person is in too
(404 otherwise).

- POST /v1/worker/runs/{id}/chat/messages: a message of the agent, in the run's thread for a chat run, else in a chat
  the agent is in (``chat_id``), in the thread of ``reply_to`` when given. A person it mentions gets the notice; an
  agent it mentions gets no work.
- POST .../chat/reactions: an emoji of the agent on a message of a chat it is in, or taken off (``remove``).
- POST .../chat/mentions: a message of the agent that mentions ``handle``, which hands that agent the work as a
  person's mention does (``chats.service.runs.hand_work``) but for the origin of the chain, one hop further, never to
  the agent's own profile. A refusal is answered (201, ``refusal``) and said in the thread. In a direct chat, an agent
  it does not have goes to the group chat of the thread (``chats.service.groups``: the one the thread opened, or one
  made now, of the origin, the agent and the agent mentioned): the message goes there, and the hub says where in the
  direct chat's thread.
- POST .../chats: a new chat of the agent and the origin of the chain, with the people named, each of whom must hold a
  grant that clears the label of the agent's project, and the agents named, which the origin may add to a chat
  (``chats.service.members.addable_agent``, ``put_agent``); the origin and each person get the notice chat_added.

The text of an agent's message, and of a mention, goes through ``agent_account_mask.mask_values``: the values of the
accounts of its profile, and every value the hub leased the run (``mask_run``), are replaced by ``***`` before it is
stored.

The agent's messages are audited as chat.message, and a chat it creates as chat.create, as the run's dispatcher with
the worker's token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.agent_accounts.service.mask import mask_run, mask_values
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.agents import (
    AgentChatIn,
    AgentHanded,
    AgentMentionIn,
    AgentMessageIn,
    AgentReactionIn,
)
from evo_agents.hub.server.chats.models.runs import Hand
from evo_agents.hub.server.chats.models.views import Chat, GroupMessage, agent_author, person_author
from evo_agents.hub.server.chats.repository import agents as store
from evo_agents.hub.server.chats.repository.access import projects_by_id
from evo_agents.hub.server.chats.repository.groups import has_agent
from evo_agents.hub.server.chats.repository.store import (
    add_member,
    emoji_kinds,
    member_event,
    put_reaction,
    take_reaction,
)
from evo_agents.hub.server.chats.repository.views import message_rows
from evo_agents.hub.server.chats.service.access import label_reader, uncleared
from evo_agents.hub.server.chats.service.groups import open_from
from evo_agents.hub.server.chats.service.members import (
    addable_agent,
    person,
    put_agent,
    refuse_clash,
    refuse_uncleared,
)
from evo_agents.hub.server.chats.service.notices import notice_added
from evo_agents.hub.server.chats.service.runs import agent_access, chat_run, hub_reply, post_agent
from evo_agents.hub.server.chats.service.views import chat_view, messages_view
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal


@dataclass(frozen=True)
class AgentRun:
    """A run whose agent acts in chats: the run, its profile, the person at the origin and the hop it is at."""

    run_id: int
    kind: str
    project_id: int
    profile: Profile
    principal: Principal  # the dispatcher (a chat run: the origin of the chain), with the worker's token
    via: str
    hop: int
    branch: str | None
    repos: list


# The run and its agent


async def agent_run(conn: AsyncConnection, user: Principal, run_id: int) -> AgentRun:
    """The run ``run_id`` the worker of ``user`` holds, of a kind whose agent uses chats; 404 or 409 otherwise."""
    worker_id = (await worker_of(conn, user))[0]
    return await held_run(conn, run_id, worker_id, user.token_id, user.kind)


async def held_run(
    conn: AsyncConnection, run_id: int, worker_id: int | None, token_id: int | None, kind: str
) -> AgentRun:
    """``agent_run`` of the run ``run_id`` that worker ``worker_id`` holds (None: the hub runtime's turn, which holds
    a run of no worker, ``hub_runtime``), acting with token ``token_id`` of ``kind``; its row locked."""
    row = await store.held_row(conn, run_id)
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind not in rules.COMMAND_KINDS:
        raise HTTPException(404, f"run {run_id} is a {row.kind} run, whose agent does not talk in chats")
    profile = None if row.profile_id is None else await by_id(conn, row.profile_id)
    if profile is None or profile.project_id != row.project_id:
        raise HTTPException(
            409, f"run {run_id} runs under an agent of another project, whose chats do not take this project's work"
        )
    person_id, login = row.dispatched_by, row.login
    found = await chat_run(conn, run_id) if row.kind == "chat" else None
    if found is not None:  # the origin of the chain, also when the run went to the workers of its type's run_as
        person_id, login = found.dispatcher_id, found.dispatcher
    # no token (None), as the hub's other machine principals: Principal types token_id int
    principal = Principal(person_id, login, False, cast(int, token_id), kind, "")
    return AgentRun(
        run_id,
        row.kind,
        row.project_id,
        profile,
        principal,
        row.dispatched_via or "machine",
        row.hop or 1,
        row.branch,
        list(row.repos or []),
    )


async def _chat_of(conn: AsyncConnection, run: AgentRun, chat_id: int | None) -> tuple[ChatAccess, int | None]:
    """(the chat the agent writes in, the root of the run's thread in it, a chat run's): the chat of a chat run when
    ``chat_id`` names none or it; 404 when the agent is not in the chat, 422 for a run that names none."""
    found = await chat_run(conn, run.run_id) if run.kind == "chat" else None
    if chat_id is not None:
        wanted = chat_id
    elif found is not None:
        wanted = found.chat_id
    else:
        raise HTTPException(422, f"run {run.run_id} is a {run.kind} run, in no chat: name one with chat_id (--chat)")
    chat = await _in_chat(conn, run, wanted)
    return chat, (found.root_id if found is not None and found.chat_id == wanted else None)


async def _in_chat(conn: AsyncConnection, run: AgentRun, chat_id: int) -> ChatAccess:
    """The agent's access to chat ``chat_id``, which both the agent and the run's dispatcher are in: the agent acts
    for that person, and only where they talk too. 404 otherwise."""
    chat = await agent_access(conn, chat_id, run.profile, run.principal.user_id)
    if chat is None:
        raise HTTPException(404, f"@{run.profile.handle} is in no chat {chat_id}: create one, or ask to be added")
    if not await store.is_member(conn, chat_id, run.principal.user_id):
        raise HTTPException(
            404,
            f"{run.principal.login}, who dispatched run {run.run_id}, is in no chat {chat_id}: the agent talks there"
            " only with them",
        )
    return chat


async def _message(conn: AsyncConnection, chat: ChatAccess, message_id: int):
    """The row of message ``message_id`` of ``chat`` when the run's dispatcher clears its label; 404 otherwise."""
    rows = await message_rows(conn, chat.chat_id, [message_id])
    reader = await label_reader(conn, chat.user_id, [row.label for row in rows])
    if not rows or not reader.sees(rows[0].label):
        raise HTTPException(404, f"chat {chat.chat_id} has no message {message_id} that the run's agent can read")
    return rows[0]


async def _thread(conn: AsyncConnection, chat: ChatAccess, root: int | None, reply_to: int | None) -> int | None:
    if reply_to is None:
        return root
    row = await _message(conn, chat, reply_to)
    return row.thread_root_id or row.id


async def _view(conn: AsyncConnection, chat: ChatAccess, message_id: int) -> GroupMessage:
    rows = await message_rows(conn, chat.chat_id, [message_id])
    (view,) = await messages_view(conn, rows, await label_reader(conn, chat.user_id, [row.label for row in rows]))
    return view


async def _audit(conn: AsyncConnection, run: AgentRun, action: str, target: str) -> None:
    who = run.principal
    await audit.record(conn, actor_id=who.user_id, token_id=who.token_id, action=action, target=target)


# The commands


async def post_message(
    conn: AsyncConnection, sealer, user: Principal, run_id: int, body: AgentMessageIn
) -> tuple[GroupMessage, int, int]:
    """A message of the agent of run ``run_id``, which the worker of ``user`` holds, in a chat it is in: the run's
    thread for a chat run. (The message as the run's dispatcher reads it, its chat's id, its id.)"""
    run = await agent_run(conn, user, run_id)
    chat, message_id = await agent_message(conn, sealer, run, body)
    return await _view(conn, chat, message_id), chat.chat_id, message_id


async def agent_message(conn: AsyncConnection, sealer, run: AgentRun, body: AgentMessageIn) -> tuple[ChatAccess, int]:
    """Post ``body`` as the agent of ``run``, masked, and audit it: (the chat, the message's id). The route of
    ``post_message`` and the hub runtime's post tool (``hub_tools``) share it."""
    chat, root = await _chat_of(conn, run, body.chat_id)
    root = await _thread(conn, chat, root, body.reply_to)
    text = await mask_values(conn, sealer, run.profile.id, body.body)
    text = await mask_run(conn, sealer, run.run_id, text)
    message_id, _ = await post_agent(conn, chat, run.profile, run.run_id, text, root, output_ids=body.output_ids)
    await _audit(conn, run, audit.CHAT_MESSAGE, f"chat:{chat.chat_id} message:{message_id} run:{run.run_id}")
    return chat, message_id


async def react(conn: AsyncConnection, user: Principal, run_id: int, body: AgentReactionIn) -> GroupMessage:
    """Put an emoji of the agent of run ``run_id`` on a message of a chat it is in, or take it off."""
    run = await agent_run(conn, user, run_id)
    chat_id = await store.chat_of_message(conn, body.message_id)
    if chat_id is None:
        raise HTTPException(404, f"no message {body.message_id} in a chat @{run.profile.handle} is in")
    chat = await _in_chat(conn, run, chat_id)
    await _message(conn, chat, body.message_id)
    by = agent_author(run.profile)
    if body.remove:
        await take_reaction(conn, chat_id, body.message_id, body.emoji, by, profile_id=run.profile.id)
    else:
        kinds = await emoji_kinds(conn, body.message_id)
        if body.emoji not in kinds and len(kinds) >= chats.MAX_EMOJI_KINDS:
            raise HTTPException(409, f"message {body.message_id} carries {len(kinds)} different emoji, the most")
        await put_reaction(conn, chat_id, body.message_id, body.emoji, by, profile_id=run.profile.id)
    return await _view(conn, chat, body.message_id)


async def _masked(conn: AsyncConnection, sealer, run: AgentRun, body: AgentMentionIn) -> str:
    """The text of the mention ``body``, masked, that names ``body.handle``: with ``@handle`` in front when it names
    it nowhere and still fits."""
    said = await mask_values(conn, sealer, run.profile.id, body.body)
    said = await mask_run(conn, sealer, run.run_id, said)
    text = said if body.handle in chats.mention_names(said) else f"@{body.handle} {said}"
    return text if len(text.encode()) <= chats.MAX_MESSAGE_BYTES else said


async def mention(conn: AsyncConnection, sealer, user: Principal, run_id: int, body: AgentMentionIn) -> AgentHanded:
    """A message of the agent of run ``run_id`` that hands the work to agent ``body.handle`` of the chat (see the
    module's docstring)."""
    run = await agent_run(conn, user, run_id)
    chat, root = await _chat_of(conn, run, body.chat_id)
    root = await _thread(conn, chat, root, body.reply_to)
    direct, opened = None, None
    if chat.kind == chats.DIRECT and not await has_agent(conn, chat.chat_id, body.handle):
        opened = await open_from(conn, chat, run.principal, root, [body.handle], run_id=run_id)
        if opened.chat_id is not None:
            direct = (chat, root)
            chat, root = await _in_chat(conn, run, opened.chat_id), None
    text = await _masked(conn, sealer, run, body)
    origin = run.principal
    author = f"@{run.profile.handle}"
    hand = Hand(chat, 0, 0, run.profile, origin.user_id, origin.login, run.via, run.hop + 1, author, text)
    message_id, kept = await post_agent(
        conn, chat, run.profile, run_id, text, root, hand=hand, target=body.handle, output_ids=body.output_ids
    )
    handed = next((item for item in kept if item["name"] == body.handle), None)
    run_made = handed.get("run_id") if handed else None
    refusal = handed["refusal"] if handed else f"no agent @{body.handle} in this chat"
    if opened is not None and body.handle in opened.refused:
        refusal = f"@{body.handle} is not in this direct chat, and no group chat could take it: "
        refusal += opened.refused[body.handle]
    if direct is not None and opened is not None:
        await hub_reply(conn, direct[0].chat_id, direct[1], opened.note())
    if refusal is not None:
        await hub_reply(conn, chat.chat_id, root or message_id, f"@{body.handle}: {refusal}")
    await _audit(conn, run, audit.CHAT_MESSAGE, f"chat:{chat.chat_id} message:{message_id} run:{run_id}")
    view = await _view(conn, chat, message_id)
    group_id = None if direct is None else chat.chat_id
    return AgentHanded(message=view, run_id=run_made, refusal=refusal, group_id=group_id)


async def create_chat(
    conn: AsyncConnection, user: Principal, run_id: int, body: AgentChatIn
) -> tuple[Chat, int, int, int]:
    """A new chat of the agent of run ``run_id`` and the person at the origin of the chain, with the people named:
    (the chat as that person reads it, its id, how many people and agents it starts with)."""
    run = await agent_run(conn, user, run_id)
    profile, origin = run.profile, run.principal
    if profile.deleted_at is not None:
        raise HTTPException(409, f"@{profile.handle} was deleted: it takes no new work")
    people = dict([await person(conn, login) for login in body.people if login.lower() != origin.login.lower()])
    others = [await addable_agent(conn, origin, handle) for handle in dict.fromkeys(body.agents)]
    others = [other for other in others if other.id != profile.id]
    refuse_clash([origin.login, *people.values()], [profile.handle, *(other.handle for other in others)])
    projects = await projects_by_id(conn, [profile.project_id])
    refuse_uncleared(await uncleared(conn, {origin.user_id: origin.login, **people}, projects), "the chat")
    chat_id = await store.insert_chat(conn, body.title, origin.user_id)
    by = f"@{profile.handle}"
    await add_member(conn, chat_id, user_id=origin.user_id, added_by=origin.user_id, added_by_run_id=run_id)
    await member_event(conn, chat_id, "member_added", person_author(origin.login), by)
    await add_member(conn, chat_id, profile_id=profile.id, added_by=origin.user_id, added_by_run_id=run_id)
    await member_event(conn, chat_id, "member_added", agent_author(profile), by)
    for user_id, login in people.items():
        await add_member(conn, chat_id, user_id=user_id, added_by=origin.user_id, added_by_run_id=run_id)
        await member_event(conn, chat_id, "member_added", person_author(login), by)
    chat = await agent_access(conn, chat_id, profile, origin.user_id)
    assert chat is not None  # the agent was added to the chat just above
    for other in others:  # the label checks of adding an agent, with the people in it now
        await put_agent(conn, origin, chat, other)
    chat = await agent_access(conn, chat_id, profile, origin.user_id)
    assert chat is not None  # and is in it still
    for user_id in (origin.user_id, *people):
        await notice_added(conn, chat, user_id)
    await _audit(conn, run, audit.CHAT_CREATE, f"chat:{chat_id} run:{run_id}")
    return await chat_view(conn, chat), chat_id, len(people) + 1, len(others) + 1
