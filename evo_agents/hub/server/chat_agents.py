"""The agent of a run in group chats (``evo_agents.hub.chat_runs``): the routes behind ``evo-agents worker chat
post|react|create|mention`` and the pull request of a chat run's branch. ``docs/workers.md`` (Chat runs) is the
protocol.

Each route takes the worker token of the worker that holds the run (404 for a run it does not hold), of a kind of
``chat_runs.COMMAND_KINDS``: a chat run, or a step, plan or author run. The run's agent is its profile, which must be of
the run's project (409 otherwise: the work of another project does not go into a chat of this one's agent), and it
acts for the person who dispatched the run, the origin of the chain: it writes only in a chat that person is in too
(404 otherwise).

- POST /v1/worker/runs/{id}/chat/messages: a message of the agent, in the run's thread for a chat run, else in a chat
  the agent is in (``chat_id``), in the thread of ``reply_to`` when given. A person it mentions gets the notice; an
  agent it mentions gets no work.
- POST .../chat/reactions: an emoji of the agent on a message of a chat it is in, or taken off (``remove``).
- POST .../chat/mentions: a message of the agent that mentions ``handle``, which hands that agent the work as a
  person's mention does (``chat_runs.hand_work``) but for the origin of the chain, one hop further, never to the agent's
  own profile, within the chat's budget. A refusal is answered (201, ``refusal``) and said in the thread.
- POST .../chats: a new chat of the agent and the origin of the chain, with the people named, each of whom must hold a
  grant that clears the label of the agent's project; the origin and each person get the notice chat_added.
- POST .../chat/pulls: the worker pushed the run's branch of a repo (chat runs only): the hub opens a draft pull
  request of it on GitHub with the workers' App, or finds the one open, and the agent's message in the thread links
  it; a repo elsewhere, or a hub without the App, gets the message without one.

The agent's messages are audited as chat.message, and a chat it creates as chat.create, as the run's dispatcher with
the worker's token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, profiles, runs, tables
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit, pulls
from evo_agents.hub.server.chat_access import ChatAccess, label_reader, projects_by_id, uncleared
from evo_agents.hub.server.chat_members import LOGIN_PATTERN, person, refuse_clash, refuse_uncleared
from evo_agents.hub.server.chat_notices import notice_added
from evo_agents.hub.server.chat_runs import Hand, agent_access, chat_run, hub_reply, post_agent
from evo_agents.hub.server.chat_store import add_member, emoji_kinds, member_event, put_reaction, take_reaction
from evo_agents.hub.server.chat_views import (
    Chat,
    GroupMessage,
    agent_author,
    chat_view,
    message_rows,
    messages_view,
    person_author,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, OBJECT_NAME, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422)}
Login = Annotated[str, Field(pattern=LOGIN_PATTERN)]
Handle = Annotated[str, Field(pattern=profiles.HANDLE_PATTERN)]
MessageRef = Annotated[int | None, Field(None, ge=1, le=MAX_ID)]

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


# Models


def _bounded(value: str) -> str:
    if not value.strip():
        raise ValueError("a message has some text")
    if len(value.encode()) > chats.MAX_MESSAGE_BYTES:
        raise ValueError(f"a message is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    return value


class AgentMessageIn(BaseModel):
    body: str = Field(min_length=1, description=f"at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    chat_id: int | None = Field(
        None, ge=1, le=MAX_ID, description="a chat the run's agent is in; null: the chat of a chat run"
    )
    reply_to: MessageRef = Field(None, description="a message whose thread it goes in; null: the run's thread")

    _body = field_validator("body")(_bounded)


class AgentMentionIn(AgentMessageIn):
    handle: Handle = Field(description="the agent of the chat the message hands work to, @ left out")


class AgentReactionIn(BaseModel):
    message_id: int = Field(ge=1, le=MAX_ID)
    emoji: str = Field(min_length=1, max_length=chats.MAX_EMOJI_CHARS)
    remove: bool = Field(False, description="take the emoji off instead")

    @field_validator("emoji")
    @classmethod
    def _one_emoji(cls, value: str) -> str:
        problem = chats.emoji_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class AgentChatIn(BaseModel):
    title: str = Field(min_length=1, max_length=chats.MAX_TITLE_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")
    people: list[Login] = Field(
        default_factory=list,
        max_length=chats.MAX_PEOPLE - 1,
        description="logins besides the person who dispatched the run, who is in it anyway",
    )


class AgentHanded(BaseModel):
    message: GroupMessage
    run_id: int | None = Field(description="the run of the agent mentioned: new, or the one of its session it woke")
    refusal: str | None = Field(description="why the mention handed no work, as the thread says too")


class ChatPushed(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    branch: str = Field(min_length=1, max_length=255, description="the run's branch, chat/<id>")
    head: str = Field(pattern=OBJECT_NAME, description="the commit the branch has now")


class ChatPull(BaseModel):
    repo: str
    branch: str
    url: str | None = Field(description="the draft pull request, or the branch; null when the hub knows neither")
    number: int | None = Field(description="the pull request's number on GitHub")
    opened: bool = Field(description="the hub opened it now; false for one open already, or none")
    message_id: int = Field(description="the agent's message in the thread that says so")


# The run and its agent


@dataclass(frozen=True)
class AgentRun:
    """A run whose agent acts in chats: the run, its profile, the person at the origin and the hop it is at."""

    run_id: int
    kind: str
    project_id: int
    profile: Profile
    principal: Principal  # the dispatcher, with the worker's token
    via: str
    hop: int
    branch: str | None
    repos: list


async def agent_run(conn: AsyncConnection, user: Principal, run_id: int) -> AgentRun:
    """The run ``run_id`` the worker of ``user`` holds, of a kind whose agent uses chats; 404 or 409 otherwise."""
    worker_id = (await worker_of(conn, user))[0]
    r, u = tables.runs, tables.users
    query = (
        select(
            r.c.state,
            r.c.worker_id,
            r.c.kind,
            r.c.project_id,
            r.c.profile_id,
            r.c.dispatched_by,
            u.c.login,
            r.c.dispatched_via,
            r.c.hop,
            r.c.branch,
            r.c.repos,
        )
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind not in rules.COMMAND_KINDS:
        raise HTTPException(404, f"run {run_id} is a {row.kind} run, whose agent does not talk in chats")
    profile = None if row.profile_id is None else await by_id(conn, row.profile_id)
    if profile is None or profile.project_id != row.project_id:
        raise HTTPException(
            409, f"run {run_id} runs under an agent of another project, whose chats do not take this project's work"
        )
    principal = Principal(row.dispatched_by, row.login, False, user.token_id, user.kind, "")
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
    if chat_id is None and found is None:
        raise HTTPException(422, f"run {run.run_id} is a {run.kind} run, in no chat: name one with chat_id (--chat)")
    wanted = chat_id if chat_id is not None else found.chat_id
    chat = await _in_chat(conn, run, wanted)
    return chat, (found.root_id if found is not None and found.chat_id == wanted else None)


async def _in_chat(conn: AsyncConnection, run: AgentRun, chat_id: int) -> ChatAccess:
    """The agent's access to chat ``chat_id``, which both the agent and the run's dispatcher are in: the agent acts
    for that person, and only where they talk too. 404 otherwise."""
    chat = await agent_access(conn, chat_id, run.profile, run.principal.user_id)
    if chat is None:
        raise HTTPException(404, f"@{run.profile.handle} is in no chat {chat_id}: create one, or ask to be added")
    m = tables.chat_members
    member = select(m.c.id).where(
        m.c.chat_id == chat_id, m.c.user_id == run.principal.user_id, m.c.removed_at.is_(None)
    )
    if (await conn.execute(member)).first() is None:
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


# The routes


@worker_router.post("/runs/{run_id}/chat/messages", status_code=201, response_model=GroupMessage, responses=REFUSALS)
async def post_message(request: Request, run_id: RunId, body: AgentMessageIn, user: CurrentUser) -> GroupMessage:
    """A message of the run's agent in a chat it is in: the run's thread for a chat run."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        chat, root = await _chat_of(conn, run, body.chat_id)
        root = await _thread(conn, chat, root, body.reply_to)
        message_id, _ = await post_agent(conn, chat, run.profile, run_id, body.body, root)
        await _audit(conn, run, audit.CHAT_MESSAGE, f"chat:{chat.chat_id} message:{message_id} run:{run_id}")
        view = await _view(conn, chat, message_id)
    log.info("chat message of an agent", extra={"run_id": run_id, "chat_id": chat.chat_id, "message_id": message_id})
    return view


@worker_router.post("/runs/{run_id}/chat/reactions", response_model=GroupMessage, responses=REFUSALS)
async def react(request: Request, run_id: RunId, body: AgentReactionIn, user: CurrentUser) -> GroupMessage:
    """Put an emoji of the run's agent on a message of a chat it is in, or take it off."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        cm = tables.chat_messages
        chat_id = (await conn.execute(select(cm.c.chat_id).where(cm.c.id == body.message_id))).scalar_one_or_none()
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


@worker_router.post("/runs/{run_id}/chat/mentions", status_code=201, response_model=AgentHanded, responses=REFUSALS)
async def mention(request: Request, run_id: RunId, body: AgentMentionIn, user: CurrentUser) -> AgentHanded:
    """A message of the run's agent that hands the work to agent ``handle`` of the chat (see the module's docstring)."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        chat, root = await _chat_of(conn, run, body.chat_id)
        root = await _thread(conn, chat, root, body.reply_to)
        text = body.body if body.handle in chats.mention_names(body.body) else f"@{body.handle} {body.body}"
        text = text if len(text.encode()) <= chats.MAX_MESSAGE_BYTES else body.body
        origin = run.principal
        author = f"@{run.profile.handle}"
        hand = Hand(chat, 0, 0, run.profile, origin.user_id, origin.login, run.via, run.hop + 1, author, text)
        message_id, kept = await post_agent(conn, chat, run.profile, run_id, text, root, hand=hand, target=body.handle)
        handed = next((item for item in kept if item["name"] == body.handle), None)
        run_made = handed.get("run_id") if handed else None
        refusal = handed["refusal"] if handed else f"no agent @{body.handle} in this chat"
        if refusal is not None:
            await hub_reply(conn, chat.chat_id, root or message_id, f"@{body.handle}: {refusal}")
        await _audit(conn, run, audit.CHAT_MESSAGE, f"chat:{chat.chat_id} message:{message_id} run:{run_id}")
        view = await _view(conn, chat, message_id)
    log.info(
        "agent mention", extra={"run_id": run_id, "handle": body.handle, "made": run_made, "refused": bool(refusal)}
    )
    return AgentHanded(message=view, run_id=run_made, refusal=refusal)


@worker_router.post("/runs/{run_id}/chats", status_code=201, response_model=Chat, responses=REFUSALS)
async def create_chat(request: Request, run_id: RunId, body: AgentChatIn, user: CurrentUser) -> Chat:
    """A new chat of the run's agent and the person at the origin of the chain, with the people named."""
    async with request.app.state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        profile, origin = run.profile, run.principal
        if profile.deleted_at is not None:
            raise HTTPException(409, f"@{profile.handle} was deleted: it takes no new work")
        people = dict([await person(conn, login) for login in body.people if login.lower() != origin.login.lower()])
        refuse_clash([origin.login, *people.values()], [profile.handle])
        projects = await projects_by_id(conn, [profile.project_id])
        refuse_uncleared(await uncleared(conn, {origin.user_id: origin.login, **people}, projects), "the chat")
        c = tables.chats
        chat_id = (
            await conn.execute(insert(c).values(title=body.title, created_by=origin.user_id).returning(c.c.id))
        ).scalar_one()
        by = f"@{profile.handle}"
        await add_member(conn, chat_id, user_id=origin.user_id, added_by=origin.user_id, added_by_run_id=run_id)
        await member_event(conn, chat_id, "member_added", person_author(origin.login), by)
        await add_member(conn, chat_id, profile_id=profile.id, added_by=origin.user_id, added_by_run_id=run_id)
        await member_event(conn, chat_id, "member_added", agent_author(profile), by)
        for user_id, login in people.items():
            await add_member(conn, chat_id, user_id=user_id, added_by=origin.user_id, added_by_run_id=run_id)
            await member_event(conn, chat_id, "member_added", person_author(login), by)
        chat = await agent_access(conn, chat_id, profile, origin.user_id)
        for user_id in (origin.user_id, *people):
            await notice_added(conn, chat, user_id)
        await _audit(conn, run, audit.CHAT_CREATE, f"chat:{chat_id} run:{run_id}")
        view = await chat_view(conn, chat)
    log.info("chat created by an agent", extra={"run_id": run_id, "chat_id": chat_id, "people": len(people) + 1})
    return view


# The pull request of a chat run's branch


async def _open_draft(app, target, branch: str, title: str, text: str) -> tuple[dict, bool]:
    """(the draft pull request of ``branch`` on ``target`` (owner, name), whether the hub opened it now). On a repo
    whose GitHub takes no draft, a pull request whose title starts with ``chat_runs.DRAFT_TITLE``."""
    owner, name = target

    async def opened(token: str) -> tuple[dict, bool]:
        found = await pulls.find_pull(app, token, owner, name, branch)
        if found is not None:
            return found, False
        held = await pulls.repository(app, token, owner, name)
        base = held.get("default_branch")
        if not isinstance(base, str) or not base:
            raise GitHubRefused(f"GitHub names no default branch of {owner}/{name}")
        pull = {"branch": branch, "base": base, "body": text}
        try:
            return await pulls.open_pull(app, token, owner, name, title=title, draft=True, **pull), True
        except pulls.Refused as exc:
            if not pulls.drafts_refused(exc.errors):
                raise
            log.info("no draft pull request on this repo", extra={"repo": f"{owner}/{name}", "why": str(exc)})
        marked = f"{rules.DRAFT_TITLE} {title}"[:240]
        return await pulls.open_pull(app, token, owner, name, title=marked, draft=False, **pull), True

    return await pulls.with_token(app, owner, name, "open", opened)


async def _pushed_run(conn: AsyncConnection, user: Principal, run_id: int, body: ChatPushed):
    """(the run, the chat run, the repo's origin) of a push the worker reports; 404 or 422 when it is not the run's."""
    run = await agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if run.kind == "chat" else None
    if found is None:
        raise HTTPException(404, f"run {run_id} is a {run.kind} run: only a chat run's branch gets a pull request here")
    if body.branch != run.branch or not rules.is_chat_branch(body.branch):
        raise HTTPException(422, f"run {run_id} pushes {run.branch}, not {body.branch}")
    if body.repo not in [entry.get("repo") for entry in run.repos if isinstance(entry, dict)]:
        raise HTTPException(422, f"run {run_id} does not work in {body.repo}")
    pr = tables.project_repos
    origin = (
        await conn.execute(select(pr.c.origin).where(pr.c.project_id == run.project_id, pr.c.name == body.repo))
    ).scalar_one_or_none()
    return run, found, origin


@worker_router.post("/runs/{run_id}/chat/pulls", status_code=201, response_model=ChatPull, responses=REFUSALS)
async def pushed(request: Request, run_id: RunId, body: ChatPushed, user: CurrentUser) -> ChatPull:
    """The worker pushed the chat run's branch of a repo: its draft pull request, linked in the thread."""
    async with request.app.state.engine.begin() as conn:
        run, found, origin = await _pushed_run(conn, user, run_id, body)
        r = tables.runs
        title = (await conn.execute(select(r.c.title).where(r.c.id == run_id))).scalar_one() or body.branch
    target = github_repo(origin) if origin else None
    app = request.app.state.github_app
    url, number, opened, problem, draft = None, None, False, None, True
    if target is not None and app is not None:
        # The handle in code spans: a bare @handle on GitHub would mention the GitHub user of that login
        text = f"Opened by the hub for `@{run.profile.handle}`, from chat {found.chat_id} (run #{run_id}). Not merged."
        try:
            made, opened = await _open_draft(app, target, body.branch, title[:240], text)
            url, number, draft = made.get("html_url"), made.get("number"), made.get("draft") is not False
        except (GitHubRefused, GitHubUnavailable) as exc:
            problem = str(exc)
            log.warning("chat run pull request not opened", extra={"run_id": run_id, "repo": body.repo, "why": problem})
    elif target is not None:
        url = f"https://github.com/{target[0]}/{target[1]}/tree/{body.branch}"
    pull = number is not None
    said = rules.pull_text(body.repo, body.branch, url, pull=pull, opened=opened, problem=problem, draft=draft)
    async with request.app.state.engine.begin() as conn:
        chat = await agent_access(conn, found.chat_id, run.profile, run.principal.user_id)
        message_id = 0
        if chat is not None:
            message_id, _ = await post_agent(conn, chat, run.profile, run_id, said, found.root_id)
    log.info("chat run branch pushed", extra={"run_id": run_id, "repo": body.repo, "pull": number, "opened": opened})
    return ChatPull(repo=body.repo, branch=body.branch, url=url, number=number, opened=opened, message_id=message_id)
