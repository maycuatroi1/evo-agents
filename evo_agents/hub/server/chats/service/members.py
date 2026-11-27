"""Who is in a group chat (``evo_agents.hub.chats``): PUT and DELETE /v1/chats/{id}/people/{login} and
/v1/chats/{id}/agents/{handle}, and the checks creating a chat makes too (``chats.service.messages``).

Any member adds a person or an agent. Adding an agent of project P needs the writer role on P; a deleted agent is
refused (409), and an agent the caller cannot see is the 404 of one that does not exist. Adding a person needs them to
clear the chat's label, and adding an agent needs every person in the chat to clear the label it takes the chat to
(``chats.service.access``): otherwise 409 names who does not, and why. A person who is a writer on P and clears P's part
by their own grant shares that part with whom it would pass (``share_part``): adding an agent of P, with the people of
the chat; adding a person, with them, for each project of the chat's agents they are a writer on; creating a chat, with
the people it starts with. So a member of P brings people outside P into a chat with P's agent, and only the parts the
one adding cannot share still refuse. The hub's own flows (triage, settle, the Orchestrator) and the agents of runs
share nothing: they add only whom the label clears. A chat holds no person and agent of the same name, so that @name
names one member (409), at most ``chats.MAX_PEOPLE`` people and ``chats.MAX_AGENTS`` agents. Adding someone already in
the chat changes nothing. A person added gets the notice chat_added.

A person leaves a chat, or its creator removes them; any member removes an agent. Each change is audited (chat.add,
chat.remove; for an agent in its project) and is an event of the chat's stream.

The members of a direct chat (``chats.service.direct``) never change: adding or removing anyone there, its person
leaving included, is refused (409, ``refuse_direct``), and a group chat is made for more people or agents.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats
from evo_agents.hub.server import audit
from evo_agents.hub.server.chats.models.access import ChatAccess, LabelProject, LabelReader, shares_part
from evo_agents.hub.server.chats.models.members import SHARE_HINT
from evo_agents.hub.server.chats.models.views import Chat, agent_author, person_author
from evo_agents.hub.server.chats.repository import members as store
from evo_agents.hub.server.chats.repository.access import grants_of
from evo_agents.hub.server.chats.repository.store import add_member, member_event, remove_member, share_label
from evo_agents.hub.server.chats.service.access import agent_projects, chat_access, chat_projects, uncleared
from evo_agents.hub.server.chats.service.notices import notice_added
from evo_agents.hub.server.chats.service.views import chat_view
from evo_agents.hub.server.profile_store import Profile, by_handle
from evo_agents.hub.server.security import Principal, holds


async def addable_agent(conn: AsyncConnection, user: Principal, handle: str) -> Profile:
    """The agent ``handle`` names, when ``user`` may add it to a chat: 404 for one they cannot see, 403 without the
    writer role on its project, 409 for a deleted one."""
    profile = await by_handle(conn, handle)
    role = None if profile is None else await store.role_on(conn, user.user_id, profile.project_id)
    if profile is None or role is None:
        raise HTTPException(404, f"no agent @{handle} that you can see: see `evo-agents hub agent list`")
    if not holds(role, "writer"):
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
    row = await store.user_named(conn, login)
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


def refuse_uncleared(found: list[str], doing: str, hint: str = "") -> None:
    if found:
        raise HTTPException(
            409,
            f"{doing} would put work of a project in front of people who do not clear its label: {'; '.join(found)}. "
            f"Nothing changed{'. ' + hint if hint else ''}",
        )


async def _record(conn, user: Principal, action: str, target: str, project_id: int | None = None) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=project_id
    )


# Adding


async def share_part(
    conn: AsyncConnection, user: Principal, chat_id: int, project: LabelProject, people: dict[int, str]
) -> list[str]:
    """Share ``project``'s part of chat ``chat_id``'s label, as ``user``, with the people of ``people`` (id: login) who
    do not clear it, when ``user`` holds the writer role on it and a grant that clears the part (``shares_part``),
    audited in the project; the logins it was shared with now, in order, none when ``user`` may not share it."""
    grants = await grants_of(conn, [user.user_id, *people], [project.id])
    held = grants.get((user.user_id, project.id))
    if held is None or not shares_part(project, *held):
        return []
    reader = {project.name: project}
    outside = {
        user_id: login
        for user_id, login in people.items()
        if user_id != user.user_id and LabelReader(user_id, reader, grants).why_not([project.label]) is not None
    }
    shared = [
        outside[user_id] for user_id in await share_label(conn, chat_id, project.id, sorted(outside), user.user_id)
    ]
    if shared:
        logins = ",".join(sorted(shared, key=str.lower))
        await _record(conn, user, audit.CHAT_SHARE, f"chat:{chat_id} shared:{logins}", project.id)
    return sorted(shared, key=str.lower)


async def _with_agent(conn: AsyncConnection, chat: ChatAccess, project_id: int) -> list[LabelProject]:
    """The projects of ``chat``'s agents with an agent of project ``project_id`` in it too, as the chat holds their
    parts now."""
    (project,) = await chat_projects(conn, chat.chat_id, [project_id])
    return [*(p for p in chat.projects if p.id != project_id), project]


async def label_problems(conn: AsyncConnection, chat: ChatAccess, profile: Profile) -> list[str]:
    """Who of the chat would not clear its label with agent ``profile`` in it, as ``uncleared`` says, one line each,
    shares the one adding it would make left out; none when it is in the chat already or everyone clears it. Changes
    nothing."""
    people, agents = await store.members_now(conn, chat.chat_id)
    if profile.handle in agents:
        return []
    return await uncleared(conn, people, await _with_agent(conn, chat, profile.project_id))


async def put_agent(
    conn: AsyncConnection, user: Principal, chat: ChatAccess, profile: Profile, *, share: bool = False
) -> bool:
    """Add agent ``profile`` to ``chat`` once the checks pass; whether it was not in it. With ``share``, a person adding
    it themselves, ``user`` shares its project's part with the people of the chat (``share_part``); the hub's own flows
    and the agents of runs share nothing."""
    refuse_direct(chat, f"adding @{profile.handle}")
    people, agents = await store.members_now(conn, chat.chat_id)
    if profile.handle in agents:
        return False
    refuse_clash(people.values(), [profile.handle])
    if len(agents) >= chats.MAX_AGENTS:
        raise HTTPException(409, f"chat {chat.chat_id} has {len(agents)} agents, the most a chat holds")
    if share:
        (project,) = await chat_projects(conn, chat.chat_id, [profile.project_id])
        await share_part(conn, user, chat.chat_id, project, people)
    projects = await _with_agent(conn, chat, profile.project_id)
    refuse_uncleared(
        await uncleared(conn, people, projects),
        f"adding @{profile.handle} of {profile.project}",
        SHARE_HINT if share else "",
    )
    added_at = await add_member(conn, chat.chat_id, profile_id=profile.id, added_by=user.user_id)
    if added_at is None:
        return False
    await member_event(conn, chat.chat_id, "member_added", agent_author(profile), user.login)
    await _record(conn, user, audit.CHAT_ADD, f"chat:{chat.chat_id} agent:@{profile.handle}", profile.project_id)
    return True


async def put_person(
    conn: AsyncConnection, user: Principal, chat: ChatAccess, user_id: int, login: str, *, share: bool = False
) -> bool:
    """Add person ``login`` to ``chat`` once the checks pass, with their notice; whether they were not in it. With
    ``share``, a person adding them themselves, ``user`` shares with them the part of each project of the chat's agents
    they may share (``share_part``); the hub's own flows share nothing."""
    refuse_direct(chat, f"adding {login}")
    people, agents = await store.members_now(conn, chat.chat_id)
    if user_id in people:
        return False
    refuse_clash([login], agents)
    if len(people) >= chats.MAX_PEOPLE:
        raise HTTPException(409, f"chat {chat.chat_id} has {len(people)} people, the most a chat holds")
    projects = chat.projects
    if share and any([await share_part(conn, user, chat.chat_id, p, {user_id: login}) for p in chat.projects]):
        projects = await agent_projects(conn, chat.chat_id)
    refuse_uncleared(await uncleared(conn, {user_id: login}, projects), f"adding {login}", SHARE_HINT if share else "")
    added_at = await add_member(conn, chat.chat_id, user_id=user_id, added_by=user.user_id)
    if added_at is None:
        return False
    await member_event(conn, chat.chat_id, "member_added", person_author(login), user.login)
    if user_id != user.user_id:
        await notice_added(conn, chat, user_id)
    await _record(conn, user, audit.CHAT_ADD, f"chat:{chat.chat_id} person:{login}")
    return True


async def add_person(conn: AsyncConnection, user: Principal, chat_id: int, login: str) -> tuple[Chat, str, bool]:
    """Add person ``login`` to chat ``chat_id`` as ``user`` (``put_person``, sharing): the chat as it reads now, their
    login as they signed in, and whether they were not in it."""
    chat = await chat_access(conn, user, chat_id, lock=True)
    refuse_direct(chat, f"adding {login}")
    user_id, found = await person(conn, login)
    added = await put_person(conn, user, chat, user_id, found, share=True)
    return await chat_view(conn, chat), found, added


async def add_agent(conn: AsyncConnection, user: Principal, chat_id: int, handle: str) -> tuple[Chat, bool]:
    """Add agent ``handle`` to chat ``chat_id`` as ``user`` (``put_agent``, sharing): the chat as it reads now, and
    whether it was not in it."""
    chat = await chat_access(conn, user, chat_id, lock=True)
    refuse_direct(chat, f"adding @{handle}")
    profile = await addable_agent(conn, user, handle)
    added = await put_agent(conn, user, chat, profile, share=True)
    return await chat_view(conn, await label_now(conn, chat)), added


# Removing


async def remove_person(conn: AsyncConnection, user: Principal, chat_id: int, login: str) -> str:
    """Take person ``login`` out of chat ``chat_id``: ``user`` leaving, or the chat's creator taking them out; their
    login as they signed in."""
    chat = await chat_access(conn, user, chat_id, lock=True)
    refuse_direct(chat, f"taking {login} out" if login.lower() != user.login.lower() else "leaving it")
    user_id, found = await person(conn, login)
    if user_id != user.user_id and user.user_id != chat.created_by:
        raise HTTPException(403, f"only {chat.creator}, who created chat {chat_id}, takes others out of it")
    if not await remove_member(conn, chat_id, user_id=user_id):
        raise HTTPException(404, f"{found} is not in chat {chat_id}")
    await member_event(conn, chat_id, "member_removed", person_author(found), user.login)
    await _record(conn, user, audit.CHAT_REMOVE, f"chat:{chat_id} person:{found}")
    return found


async def remove_agent(conn: AsyncConnection, user: Principal, chat_id: int, handle: str) -> None:
    """Take agent ``handle`` out of chat ``chat_id`` as ``user``."""
    refuse_direct(await chat_access(conn, user, chat_id, lock=True), f"taking @{handle} out")
    profile = await by_handle(conn, handle)
    if profile is None or not await remove_member(conn, chat_id, profile_id=profile.id):
        raise HTTPException(404, f"@{handle} is not in chat {chat_id}")
    await member_event(conn, chat_id, "member_removed", agent_author(profile), user.login)
    await _record(conn, user, audit.CHAT_REMOVE, f"chat:{chat_id} agent:@{handle}", profile.project_id)


async def label_now(conn: AsyncConnection, chat: ChatAccess) -> ChatAccess:
    """``chat`` with the projects of its agents as they are now, after a change of its agents."""
    return replace(chat, projects=await agent_projects(conn, chat.chat_id))
