"""Whom a person may add to a group chat (``evo_agents.hub.chats``): GET /v1/chats/people, the list New chat and a
chat's Members search and pick from.

The people are those the caller sees already: everyone who holds a grant on a project the caller holds one on, and
everyone in a chat the caller is in, the caller left out, by login ignoring case, at most ``MAX_CANDIDATES``. A person
outside both is still added by login (``chats.service.members.person``); the list only spares typing one.

Given the handles of a chat's agents (``agents``, those it starts with or has), each person says why they could not be
in it (``why``): the words ``chats.service.access.uncleared`` gives when the hub refuses it, which name only projects
the caller holds a grant on. A handle of no agent, or of one whose project the caller holds no grant on, counts for
nothing, and neither does a project whose part the caller would share with the person they add
(``chats.models.access.shares_part``: the writer role and a grant that clears it). Reading the list changes nothing and
is not audited.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import LabelReader, chat_label, shares_part
from evo_agents.hub.server.chats.models.people import ChatCandidate
from evo_agents.hub.server.chats.repository import people as store
from evo_agents.hub.server.chats.repository.access import grants_of, projects_by_id
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.security import Principal


async def _projects(conn: AsyncConnection, user: Principal, handles: list[str]) -> list:
    """The projects of the agents ``handles`` names whose project ``user`` holds a grant on."""
    found = [profile for handle in dict.fromkeys(handles) if (profile := await by_handle(conn, handle)) is not None]
    if not found:
        return []
    granted = await grants_of(conn, [user.user_id], [profile.project_id for profile in found])
    return await projects_by_id(conn, [p.project_id for p in found if (user.user_id, p.project_id) in granted])


async def candidates(engine: AsyncEngine, user: Principal, handles: list[str]) -> list[ChatCandidate]:
    """The people ``user`` may pick for a chat with the agents ``handles``, as the module says, by login."""
    async with UnitOfWork(engine) as conn:
        rows = await store.people_seen(conn, user.user_id)
        projects = await _projects(conn, user, handles)
        held = await grants_of(conn, [user.user_id], [project.id for project in projects])
        projects = [p for p in projects if not shares_part(p, *held.get((user.user_id, p.id), (None, None)))]
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
