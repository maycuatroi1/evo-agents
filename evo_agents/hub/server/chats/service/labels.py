"""The level of a group chat's label (``chats.service.access``): PUT and DELETE /v1/chats/{id}/label.

A chat's label is its agents' projects' default labels. An admin of every one of those projects may set the chat's own
level, at or below each project's default level and on each one's ladder: the part of each project in the chat then
carries that level, so its messages may leave the hub at it (a sink of kind channel,
``chats.models.access.channel_allows``). The level set names the projects it lowers; a project whose agent joins later
keeps its default label. DELETE takes the level off, and the label is its projects' default again.

Someone who is not an admin of every project gets 403, a level a project's ladder lacks or above its default 422, a
direct chat or a chat without agents 409. A change that would raise the label past a person in the chat, taking the
level off say, is refused (409) and names who, as adding an agent is. A message keeps the label it was sent with: one
sent before the change keeps the old label, one sent after it carries the new one, and who may be in the chat reads the
new one. Setting the level a chat has already changes nothing; each change is audited as chat.label
("chat:<id> label:<level>", or ``label:default``).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import ChatAccess, with_override, with_shares
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.repository import labels as store
from evo_agents.hub.server.chats.repository.access import label_shares, projects_by_id
from evo_agents.hub.server.chats.service.access import chat_access, label_admin, uncleared
from evo_agents.hub.server.chats.service.members import refuse_uncleared
from evo_agents.hub.server.chats.service.views import chat_view
from evo_agents.hub.server.security import Principal


def _settable(chat: ChatAccess) -> ChatAccess:
    if chat.kind == chats.DIRECT:
        raise HTTPException(409, f"chat {chat.chat_id} is a direct chat: its label is its agent's project's own")
    if not chat.projects:
        raise HTTPException(409, f"chat {chat.chat_id} has no agents: its label names no project to set a level of")
    return chat


async def _refuse_non_admin(conn: AsyncConnection, user: Principal, chat: ChatAccess) -> None:
    if user.scope is not None or not await label_admin(conn, chat):
        names = ", ".join(project.name for project in chat.projects)
        raise HTTPException(
            403,
            f"setting the level of chat {chat.chat_id} needs the admin role on every project of its agents: {names}",
        )


def _refuse_level(chat: ChatAccess, level: str) -> None:
    problems = [
        f"project {p.name} has no level {level!r}"
        if level not in p.levels
        else f"{level!r} is above the default level {p.default_level!r} of project {p.name}"
        for p in chat.projects
        if not p.lowers_to(level)
    ]
    if problems:
        raise HTTPException(
            422, f"chat {chat.chat_id} cannot be set to {level!r}: {'; '.join(problems)}. Nothing changed"
        )


async def _change(conn: AsyncConnection, user: Principal, chat: ChatAccess, level: str | None) -> bool:
    """Make ``level`` (None: the default) the chat's level once the checks pass; whether it changed."""
    _settable(chat)
    await _refuse_non_admin(conn, user, chat)
    if level is not None:
        _refuse_level(chat, level)
    override = None if level is None else {"level": level, "projects": sorted(p.name for p in chat.projects)}
    if override == chat.label_override:
        return False
    people = await store.people_in(conn, chat.chat_id)
    projects = with_override(await projects_by_id(conn, [p.id for p in chat.projects]), override)
    projects = with_shares(projects, await label_shares(conn, [chat.chat_id]), chat.chat_id)  # as the new level reads
    refuse_uncleared(await uncleared(conn, people, projects), f"setting the level of chat {chat.chat_id}")
    await store.set_override(conn, chat.chat_id, override)
    target = f"chat:{chat.chat_id} label:{level or 'default'}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_LABEL, target=target)
    return True


async def set_level(engine: AsyncEngine, user: Principal, chat_id: int, level: str | None) -> tuple[Chat, bool]:
    """Make ``level`` (None: the default) the level of chat ``chat_id`` for ``user``: the chat as it reads now, and
    whether it changed."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        changed = await _change(conn, user, chat, level)
        return await chat_view(conn, await chat_access(conn, user, chat_id) if changed else chat), changed
