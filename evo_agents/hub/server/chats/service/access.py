"""Who may read and write a group chat (``evo_agents.hub.chats``), and the label rule of chats.

Only the chat's members read it and write to it: someone who is not one, or left, gets the 404 of a chat that does not
exist, so an id tells nothing about the chats one is not in.

A chat's label is the labels of the projects of its agents: for each project with an agent in the chat, the project's
default label, the label its runs on no plan read as (``runs.service.views.sees_unplanned``), by names, naming that
project alone. A chat without agents has an empty label: it holds no project's work. A person clears a label when they
hold a grant on every project it names, whose max level reaches that project's label on the project's own ladder
(``ProjectRules.visible_by_grant``), or when a writer on the project shared its part with them in that chat.

A writer on project P shares P's part of a chat's label with the people of the chat who do not clear it when they add
an agent of P to it, and with a person they add to a chat with an agent of P (chat_label_shares), so a member of P
brings people outside P into a chat with P's agent. The writer must clear the part by their own grant, as must whoever
shares it: a person cleared by a share alone shares nothing, and someone they add who holds no grant is refused. The
part names the people it was shared with (``shared_with``, their ids), while the writer who shared it still holds the
writer role on P and a grant that clears the part; a share goes with its person when they leave the chat, and with P
when P has no agent left in it (``chats.repository.store.unshare``).

Every person in a chat clears its label: adding an agent whose project would take it past someone in the chat is
refused, and so is adding someone who does not clear it, unless the one adding shares the part as above. The hub
checks it again on every read and write, from the projects, grants and shares as they are then, so someone whose grant
was lowered or revoked, or whose share's writer lost theirs, is refused (403), fail closed. A message carries the label
the chat had when it was sent (``LabelReader``), the people its parts were shared with included: once an agent left and
the label went down, a person who joined since does not read what was written before, nor what was written before a
part was shared with them.

An admin of every project with an agent in a group chat may set the chat's own level (``chats.service.labels``): at or below
each project's default level, it replaces the level of the part of each project in the chat when it was set
(``chats.label_override``, {level, projects}), so the chat's label goes down and its messages may leave the hub at that
level (``channel_allows``). A project whose agent joins later keeps its default label, as no admin of it lowered it.
Every rule above reads the label with the level set: who may be in the chat, and the label a message gets.

A chat is restricted when a project of its agents is (``telegram.restricted``: its hub sink cleared at or above the
level customer or the location domestic-only, or no hub sink): its notices name the chat and link to it, no more. This
rule of a member's own channel reads the hub sink only, never a sink of kind channel.

A sink of kind channel (``access.CHANNEL_KIND``) names a kind of channel of a core plugin by its id (``telegram``,
say): what a chat of the project may send out to a group of that channel, read by ``channel_allows``. It fails closed:
a project without a sink of kind channel for that kind lets nothing of its out there.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import (
    NOT_A_MEMBER,
    ChatAccess,
    LabelProject,
    LabelReader,
    channel_allows,
    chat_label,
    with_override,
    with_shares,
)
from evo_agents.hub.server.chats.repository import access as store
from evo_agents.hub.server.security import Principal


async def channel_cleared(conn: AsyncConnection, label, kind: str) -> bool:
    """``channel_allows`` for ``label``, with the projects it names as the hub holds them now."""
    names = [
        item["projects"][0]
        for item in label or []
        if isinstance(item, Mapping) and isinstance(item.get("projects"), list) and item["projects"]
    ]
    return channel_allows(label, kind, await store.projects_named(conn, names))


async def chat_projects(conn: AsyncConnection, chat_id: int, project_ids: Iterable[int]) -> list[LabelProject]:
    """The projects ``project_ids`` as chat ``chat_id`` holds their parts now: at the level the chat set for each, if
    any, with the people each was shared with there."""
    ids = sorted(set(project_ids))
    if not ids:
        return []
    found = with_override(await store.projects_by_id(conn, ids), await store.label_override(conn, chat_id))
    return with_shares(found, await store.label_shares(conn, [chat_id]), chat_id)


async def agent_projects(conn: AsyncConnection, chat_id: int) -> tuple[LabelProject, ...]:
    """The projects of the agents in chat ``chat_id`` now, at the level the chat set for each, if any, with the people
    each was shared with there."""
    ids = await store.agent_project_ids(conn, chat_id)
    return tuple(sorted(await chat_projects(conn, chat_id, ids), key=lambda p: p.name))


async def cleared_chats(conn: AsyncConnection, user_id: int) -> list[int]:
    """The chats person ``user_id`` is in and clears the label of now, as ``chat_access`` lets them read, by id."""
    chat_ids = await store.member_chats(conn, user_id)
    if not chat_ids:
        return []
    agents = await store.chat_agent_projects(conn, chat_ids)
    projects = {p.id: p for p in await store.projects_by_id(conn, [row.project_id for row in agents])}
    grants = await store.grants_of(conn, [user_id], list(projects))
    by_chat: dict[int, list[LabelProject]] = {chat_id: [] for chat_id in chat_ids}
    for row in agents:
        if row.project_id in projects:
            by_chat[row.chat_id].append(projects[row.project_id])
    overrides = await store.label_overrides(conn, chat_ids)
    shares = await store.label_shares(conn, chat_ids)
    reader = LabelReader(user_id, {p.name: p for p in projects.values()}, grants)
    return [
        chat_id
        for chat_id, found in by_chat.items()
        if reader.why_not(chat_label(with_shares(with_override(found, overrides.get(chat_id)), shares, chat_id)))
        is None
    ]


async def uncleared(conn: AsyncConnection, user_ids: dict[int, str], projects: Iterable[LabelProject]) -> list[str]:
    """Why each person of ``user_ids`` (id: login) who does not clear the label of ``projects`` does not, in login
    order; empty when every one clears it."""
    chosen = {project.name: project for project in projects}
    grants = await store.grants_of(conn, user_ids, [p.id for p in chosen.values()])
    label = chat_label(chosen.values())
    found = []
    for user_id, login in sorted(user_ids.items(), key=lambda item: item[1].lower()):
        why = LabelReader(user_id, chosen, grants).why_not(label)
        if why is not None:
            found.append(f"{login} ({why})")
    return found


async def label_reader(conn: AsyncConnection, user_id: int, labels: Iterable[list]) -> LabelReader:
    """A reader of the labels ``labels`` for person ``user_id``: the projects they name and the person's grants."""
    names = {
        item["projects"][0]
        for label in labels
        for item in label
        if isinstance(item, dict) and isinstance(item.get("projects"), list) and item["projects"]
    }
    projects = await store.projects_named(conn, names)
    grants = await store.grants_of(conn, [user_id], [p.id for p in projects.values()])
    return LabelReader(user_id, projects, grants)


async def chat_access(conn: AsyncConnection, user: Principal, chat_id: int, *, lock: bool = False) -> ChatAccess:
    """``user``'s access to chat ``chat_id``: 404 unless they are in it, 403 when they no longer clear its label. With
    ``lock`` the chat's row is locked, as a change of its members needs."""
    row = await store.membership(conn, chat_id, user.user_id, lock=lock)
    if row is None:
        raise HTTPException(404, NOT_A_MEMBER.format(id=chat_id))
    projects = await agent_projects(conn, chat_id)
    found = ChatAccess(
        chat_id=row.id,
        title=row.title,
        created_by=row.created_by,
        creator=row.creator,
        created_at=row.created_at,
        event_seq=row.event_seq,
        member_id=row.member_id,
        last_read_seq=row.last_read_seq,
        user_id=user.user_id,
        login=user.login,
        projects=projects,
        kind=row.kind,
        label_override=row.label_override,
    )
    grants = await store.grants_of(conn, [user.user_id], [p.id for p in projects])
    why = LabelReader(user.user_id, {p.name: p for p in projects}, grants).why_not(found.label)
    if why is not None:
        raise HTTPException(
            403,
            f"chat {chat_id} holds the work of projects {', '.join(p.name for p in projects)}, and you no longer "
            f"clear its label: {why}. Ask an admin of the project for a grant that reaches it",
        )
    return found


async def check_access(engine: AsyncEngine, user: Principal, chat_id: int) -> None:
    """The refusals of ``chat_access`` for ``user`` and chat ``chat_id``, in a transaction of their own: a stream checks
    them before it starts, so a refusal is an ordinary error answer."""
    async with UnitOfWork(engine) as conn:
        await chat_access(conn, user, chat_id)


async def label_admin(conn: AsyncConnection, chat: ChatAccess) -> bool:
    """Whether the reader of ``chat`` is an admin of every project of its agents, as setting its level needs."""
    if not chat.projects:
        return False
    grants = await store.grants_of(conn, [chat.user_id], [p.id for p in chat.projects])
    return all(grants.get((chat.user_id, p.id), (None,))[0] == "admin" for p in chat.projects)
