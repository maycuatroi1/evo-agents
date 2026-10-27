"""Who is in a group chat (``evo_agents.hub.chats``): PUT and DELETE /v1/chats/{id}/people/{login} and
/v1/chats/{id}/agents/{handle}, and the checks creating a chat makes too (``chats.create_chat``).

Any member adds a person or an agent. Adding an agent of project P needs the writer role on P; a deleted agent is
refused (409), and an agent the caller cannot see is the 404 of one that does not exist. Adding a person needs them to
clear the chat's label, and adding an agent needs every person in the chat to clear the label it takes the chat to
(``chat_access``): otherwise 409 names who does not, and why. A chat holds no person and agent of the same name, so
that @name names one member (409), at most ``chats.MAX_PEOPLE`` people and ``chats.MAX_AGENTS`` agents. Adding someone
already in the chat changes nothing. A person added gets the notice chat_added.

A person leaves a chat, or its creator removes them; any member removes an agent. Each change is audited (chat.add,
chat.remove; for an agent in its project) and is an event of the chat's stream.

The members of a direct chat (``chat_direct``) never change: adding or removing anyone there, its person leaving
included, is refused (409, ``refuse_direct``), and a group chat is made for more people or agents.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, profiles, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_access import (
    ChatAccess,
    agent_projects,
    chat_access,
    projects_by_id,
    uncleared,
    with_override,
)
from evo_agents.hub.server.chat_notices import notice_added
from evo_agents.hub.server.chat_store import add_member, member_event, remove_member
from evo_agents.hub.server.chat_views import Chat, agent_author, chat_view, person_author
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

LOGIN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$"  # a GitHub login
ChatId = Annotated[int, Path(ge=1, le=MAX_ID)]
LoginPath = Annotated[str, Path(pattern=LOGIN_PATTERN, description="a person's login")]
HandlePath = Annotated[str, Path(pattern=profiles.HANDLE_PATTERN, description="an agent's handle, @ left out")]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


# The checks


async def addable_agent(conn: AsyncConnection, user: Principal, handle: str) -> Profile:
    """The agent ``handle`` names, when ``user`` may add it to a chat: 404 for one they cannot see, 403 without the
    writer role on its project, 409 for a deleted one."""
    profile = await by_handle(conn, handle)
    g = tables.grants
    role = None
    if profile is not None:
        query = select(g.c.role).where(g.c.user_id == user.user_id, g.c.project_id == profile.project_id)
        role = (await conn.execute(query)).scalar_one_or_none()
    if profile is None or role is None:
        raise HTTPException(404, f"no agent @{handle} that you can see: see `evo-agents hub agent list`")
    if not has_role(role, "writer"):
        raise HTTPException(
            403,
            f"adding @{handle} to a chat needs the writer role on project {profile.project}, whose agent it is; "
            f"you hold {role}",
        )
    if profile.deleted_at is not None:
        raise HTTPException(409, f"@{handle} was deleted at {profile.deleted_at.isoformat()}: it takes no new work")
    return profile


async def person(conn: AsyncConnection, login: str) -> tuple[int, str]:
    """(id, login) of the person who signed in to the hub as ``login``, ignoring case; 404 for nobody."""
    u = tables.users
    row = (await conn.execute(select(u.c.id, u.c.login).where(func.lower(u.c.login) == login.lower()))).one_or_none()
    if row is None:
        raise HTTPException(404, f"nobody signed in to this hub as {login}: they sign in once before they are added")
    return row.id, row.login


def refuse_clash(logins, handles) -> None:
    """409 when a login is also a handle: @name would name either."""
    both = sorted({login.lower() for login in logins} & set(handles))
    if both:
        raise HTTPException(
            409,
            f"{', '.join(both)} would be both a person and an agent of the chat, and @{both[0]} would name either: a "
            "chat holds one of them",
        )


def refuse_direct(chat: ChatAccess, doing: str) -> None:
    """409 when ``chat`` is a direct chat, whose members never change."""
    if chat.kind == chats.DIRECT:
        raise HTTPException(
            409,
            f"chat {chat.chat_id} is a direct chat of one person and one agent, whose members never change: {doing} "
            "is refused. Start a group chat (New chat, or POST /v1/chats) to talk with more people or agents",
        )


def refuse_uncleared(found: list[str], doing: str) -> None:
    if found:
        raise HTTPException(
            409,
            f"{doing} would put work of a project in front of people who do not clear its label: {'; '.join(found)}. "
            "Nothing changed",
        )


async def _members(conn: AsyncConnection, chat_id: int) -> tuple[dict[int, str], list[str]]:
    """({person id: login}, [agent handle]) of the chat now."""
    m, u, ap = tables.chat_members, tables.users, tables.agent_profiles
    query = (
        select(m.c.user_id, u.c.login, ap.c.handle)
        .select_from(m.outerjoin(u, u.c.id == m.c.user_id).outerjoin(ap, ap.c.id == m.c.profile_id))
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    )
    people, agents = {}, []
    for row in await conn.execute(query):
        if row.user_id is not None:
            people[row.user_id] = row.login
        else:
            agents.append(row.handle)
    return people, agents


async def _record(conn, user: Principal, action: str, target: str, project_id: int | None = None) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=project_id
    )


# Adding


async def label_problems(conn: AsyncConnection, chat: ChatAccess, profile: Profile) -> list[str]:
    """Who of the chat would not clear its label with agent ``profile`` in it, as ``uncleared`` says, one line each;
    none when it is in the chat already or everyone clears it. Changes nothing."""
    people, agents = await _members(conn, chat.chat_id)
    if profile.handle in agents:
        return []
    projects = with_override([*chat.projects, *await projects_by_id(conn, [profile.project_id])], chat.label_override)
    return await uncleared(conn, people, projects)


async def put_agent(conn: AsyncConnection, user: Principal, chat: ChatAccess, profile: Profile) -> bool:
    """Add agent ``profile`` to ``chat`` once the checks pass; whether it was not in it."""
    refuse_direct(chat, f"adding @{profile.handle}")
    people, agents = await _members(conn, chat.chat_id)
    if profile.handle in agents:
        return False
    refuse_clash(people.values(), [profile.handle])
    if len(agents) >= chats.MAX_AGENTS:
        raise HTTPException(409, f"chat {chat.chat_id} has {len(agents)} agents, the most a chat holds")
    projects = with_override([*chat.projects, *await projects_by_id(conn, [profile.project_id])], chat.label_override)
    refuse_uncleared(await uncleared(conn, people, projects), f"adding @{profile.handle} of {profile.project}")
    added_at = await add_member(conn, chat.chat_id, profile_id=profile.id, added_by=user.user_id)
    if added_at is None:
        return False
    await member_event(conn, chat.chat_id, "member_added", agent_author(profile), user.login)
    await _record(conn, user, audit.CHAT_ADD, f"chat:{chat.chat_id} agent:@{profile.handle}", profile.project_id)
    return True


async def put_person(conn: AsyncConnection, user: Principal, chat: ChatAccess, user_id: int, login: str) -> bool:
    """Add person ``login`` to ``chat`` once the checks pass, with their notice; whether they were not in it."""
    refuse_direct(chat, f"adding {login}")
    people, agents = await _members(conn, chat.chat_id)
    if user_id in people:
        return False
    refuse_clash([login], agents)
    if len(people) >= chats.MAX_PEOPLE:
        raise HTTPException(409, f"chat {chat.chat_id} has {len(people)} people, the most a chat holds")
    refuse_uncleared(await uncleared(conn, {user_id: login}, chat.projects), f"adding {login}")
    added_at = await add_member(conn, chat.chat_id, user_id=user_id, added_by=user.user_id)
    if added_at is None:
        return False
    await member_event(conn, chat.chat_id, "member_added", person_author(login), user.login)
    if user_id != user.user_id:
        await notice_added(conn, chat, user_id)
    await _record(conn, user, audit.CHAT_ADD, f"chat:{chat.chat_id} person:{login}")
    return True


@router.put("/{chat_id}/people/{login}", response_model=Chat, responses=REFUSALS)
async def add_person(request: Request, chat_id: ChatId, login: LoginPath, user: CurrentUser) -> Chat:
    """Add a person to the chat; nothing changes when they are in it."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        refuse_direct(chat, f"adding {login}")
        user_id, found = await person(conn, login)
        added = await put_person(conn, user, chat, user_id, found)
        view = await chat_view(conn, chat)
    log.info("chat person added", extra={"chat_id": chat_id, "login": found, "added": added})
    return view


@router.put("/{chat_id}/agents/{handle}", response_model=Chat, responses=REFUSALS)
async def add_agent(request: Request, chat_id: ChatId, handle: HandlePath, user: CurrentUser) -> Chat:
    """Add an agent to the chat (the writer role on its project); nothing changes when it is in it."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        refuse_direct(chat, f"adding @{handle}")
        profile = await addable_agent(conn, user, handle)
        added = await put_agent(conn, user, chat, profile)
        view = await chat_view(conn, await label_now(conn, chat))
    log.info("chat agent added", extra={"chat_id": chat_id, "handle": handle, "added": added})
    return view


# Removing


@router.delete("/{chat_id}/people/{login}", status_code=204, responses=REFUSALS)
async def remove_person(request: Request, chat_id: ChatId, login: LoginPath, user: CurrentUser) -> Response:
    """Leave the chat, or, for its creator, take a person out of it."""
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        refuse_direct(chat, f"taking {login} out" if login.lower() != user.login.lower() else "leaving it")
        user_id, found = await person(conn, login)
        if user_id != user.user_id and user.user_id != chat.created_by:
            raise HTTPException(403, f"only {chat.creator}, who created chat {chat_id}, takes others out of it")
        if not await remove_member(conn, chat_id, user_id=user_id):
            raise HTTPException(404, f"{found} is not in chat {chat_id}")
        await member_event(conn, chat_id, "member_removed", person_author(found), user.login)
        await _record(conn, user, audit.CHAT_REMOVE, f"chat:{chat_id} person:{found}")
    log.info("chat person removed", extra={"chat_id": chat_id, "login": found})
    return Response(status_code=204)


@router.delete("/{chat_id}/agents/{handle}", status_code=204, responses=REFUSALS)
async def remove_agent(request: Request, chat_id: ChatId, handle: HandlePath, user: CurrentUser) -> Response:
    """Take an agent out of the chat."""
    async with request.app.state.engine.begin() as conn:
        refuse_direct(await chat_access(conn, user, chat_id, lock=True), f"taking @{handle} out")
        profile = await by_handle(conn, handle)
        if profile is None or not await remove_member(conn, chat_id, profile_id=profile.id):
            raise HTTPException(404, f"@{handle} is not in chat {chat_id}")
        await member_event(conn, chat_id, "member_removed", agent_author(profile), user.login)
        await _record(conn, user, audit.CHAT_REMOVE, f"chat:{chat_id} agent:@{handle}", profile.project_id)
    log.info("chat agent removed", extra={"chat_id": chat_id, "handle": handle})
    return Response(status_code=204)


async def label_now(conn: AsyncConnection, chat: ChatAccess) -> ChatAccess:
    """``chat`` with the projects of its agents as they are now, after a change of its agents."""
    return replace(chat, projects=await agent_projects(conn, chat.chat_id))
