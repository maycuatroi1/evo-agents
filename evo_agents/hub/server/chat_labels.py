"""The level of a group chat's label (``chat_access``): PUT and DELETE /v1/chats/{id}/label.

A chat's label is its agents' projects' default labels. An admin of every one of those projects may set the chat's own
level, at or below each project's default level and on each one's ladder: the part of each project in the chat then
carries that level, so its messages may leave the hub at it (a sink of kind channel, ``chat_access.channel_allows``).
The level set names the projects it lowers; a project whose agent joins later keeps its default label. DELETE takes the
level off, and the label is its projects' default again.

Someone who is not an admin of every project gets 403, a level a project's ladder lacks or above its default 422, a
direct chat or a chat without agents 409. A change that would raise the label past a person in the chat, taking the
level off say, is refused (409) and names who, as adding an agent is. A message keeps the label it was sent with: one
sent before the change keeps the old label, one sent after it carries the new one, and who may be in the chat reads the
new one. Setting the level a chat has already changes nothing; each change is audited as chat.label
("chat:<id> label:<level>", or ``label:default``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_access import (
    ChatAccess,
    chat_access,
    label_admin,
    projects_by_id,
    uncleared,
    with_override,
)
from evo_agents.hub.server.chat_members import ChatId, refuse_uncleared
from evo_agents.hub.server.chat_views import Chat, chat_view
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
LEVEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


class ChatLabelIn(BaseModel):
    level: str = Field(
        pattern=LEVEL_PATTERN,
        description="the chat's level: on the ladder of every project of its agents, at or below each one's default",
    )


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
    m, u = tables.chat_members, tables.users
    query = select(u.c.id, u.c.login).join_from(m, u, u.c.id == m.c.user_id)
    people = dict((await conn.execute(query.where(m.c.chat_id == chat.chat_id, m.c.removed_at.is_(None)))).all())
    projects = with_override(await projects_by_id(conn, [p.id for p in chat.projects]), override)
    refuse_uncleared(await uncleared(conn, people, projects), f"setting the level of chat {chat.chat_id}")
    c = tables.chats
    await conn.execute(
        update(c).values(label_override=null() if override is None else override).where(c.c.id == chat.chat_id)
    )
    target = f"chat:{chat.chat_id} label:{level or 'default'}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_LABEL, target=target)
    return True


async def _set(request: Request, chat_id: int, user: Principal, level: str | None) -> Chat:
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        changed = await _change(conn, user, chat, level)
        view = await chat_view(conn, await chat_access(conn, user, chat_id) if changed else chat)
    log.info("chat label changed", extra={"chat_id": chat_id, "set": level is not None, "changed": changed})
    return view


@router.put("/{chat_id}/label", response_model=Chat, responses=REFUSALS)
async def put_label(request: Request, chat_id: ChatId, body: ChatLabelIn, user: CurrentUser) -> Chat:
    """Set the chat's level, at or below the default level of every project of its agents: a message sent from now
    on carries it. Needs the admin role on every one of those projects."""
    return await _set(request, chat_id, user, body.level)


@router.delete("/{chat_id}/label", response_model=Chat, responses=REFUSALS)
async def delete_label(request: Request, chat_id: ChatId, user: CurrentUser) -> Chat:
    """Take the chat's level off: its label is its projects' default again, for the messages sent from now on."""
    return await _set(request, chat_id, user, None)
