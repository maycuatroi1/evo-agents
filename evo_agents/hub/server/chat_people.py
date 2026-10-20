"""Whom a person may add to a group chat (``evo_agents.hub.chats``): GET /v1/chats/people, the list New chat and a
chat's Members search and pick from.

The people are those the caller sees already: everyone who holds a grant on a project the caller holds one on, and
everyone in a chat the caller is in, the caller left out, by login ignoring case, at most ``MAX_CANDIDATES``. A person
outside both is still added by login (``chat_members.person``); the list only spares typing one.

Given the handles of a chat's agents (``agents``, those it starts with or has), each person says why they could not be
in it (``why``): the words ``chat_access.uncleared`` gives when the hub refuses it, which name only projects the caller
holds a grant on. A handle of no agent, or of one whose project the caller holds no grant on, counts for nothing.
Reading the list changes nothing and is not audited.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, profiles, tables
from evo_agents.hub.server.chat_access import LabelReader, chat_label, grants_of, projects_by_id
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_CANDIDATES = 500

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


class ChatCandidate(BaseModel):
    login: str
    avatar_url: str | None = Field(description="their GitHub avatar; null for a login that never signed in")
    why: str | None = Field(
        description="why they could not be in a chat with the agents given, as the hub's refusal says it; null when "
        "they could"
    )


async def _projects(conn: AsyncConnection, user: Principal, handles: list[str]) -> list:
    """The projects of the agents ``handles`` names whose project ``user`` holds a grant on."""
    found = [profile for handle in dict.fromkeys(handles) if (profile := await by_handle(conn, handle)) is not None]
    if not found:
        return []
    granted = await grants_of(conn, [user.user_id], [profile.project_id for profile in found])
    return await projects_by_id(conn, [p.project_id for p in found if (user.user_id, p.project_id) in granted])


async def candidates(conn: AsyncConnection, user: Principal, handles: list[str]) -> list[ChatCandidate]:
    """The people ``user`` may pick for a chat with the agents ``handles``, as the module says, by login."""
    u, g, m = tables.users, tables.grants, tables.chat_members
    mine = select(g.c.project_id).where(g.c.user_id == user.user_id)
    sharing = select(g.c.user_id).where(g.c.project_id.in_(mine))
    my_chats = select(m.c.chat_id).where(m.c.user_id == user.user_id, m.c.removed_at.is_(None))
    chatting = select(m.c.user_id).where(m.c.chat_id.in_(my_chats), m.c.removed_at.is_(None), m.c.user_id.is_not(None))
    query = (
        select(u.c.id, u.c.login, u.c.github_id)
        .where(or_(u.c.id.in_(sharing), u.c.id.in_(chatting)), u.c.id != user.user_id)
        .order_by(func.lower(u.c.login))
        .limit(MAX_CANDIDATES)
    )
    rows = (await conn.execute(query)).all()
    projects = await _projects(conn, user, handles)
    label = chat_label(projects)
    named = {project.name: project for project in projects}
    grants = await grants_of(conn, [row.id for row in rows], [project.id for project in projects])
    return [
        ChatCandidate(
            login=row.login,
            avatar_url=chats.github_avatar(row.github_id),
            why=LabelReader(row.id, named, grants).why_not(label),
        )
        for row in rows
    ]


@router.get("/people", response_model=list[ChatCandidate])
async def list_candidates(
    request: Request,
    user: CurrentUser,
    agents: Annotated[
        list[str],
        Query(max_length=chats.MAX_AGENTS, description="a handle of the chat's agents, @ left out; repeat it"),
    ] = [],  # noqa: B006
) -> list[ChatCandidate]:
    """The people the caller may add to a chat, by login, each with why they could not be in it with ``agents``."""
    handles = [handle for handle in agents if profiles.HANDLE.fullmatch(handle)]
    async with request.app.state.engine.begin() as conn:
        found = await candidates(conn, user, handles)
    log.info("chat candidates read", extra={"people": len(found), "agents": len(handles)})
    return found
