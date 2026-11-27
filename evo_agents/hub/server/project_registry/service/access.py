"""A caller's access to one project (``evo_agents.hub.server.projects``), where the routes for memories, plans, skills
and the knowledge graph start: the project's rules and the caller's grant, capped for the agent of a run by its scope
(``scoped_grant``). It imports nothing that reaches ``evo_agents.hub.server.base.scope``, which imports it."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import ROLES, ProjectRules
from evo_agents.hub.server.project_registry.models.projects import ProjectAccess, not_found
from evo_agents.hub.server.project_registry.repository import projects as repository
from evo_agents.hub.server.security import Principal


def lower(ladder, held, cap):
    """The lower of ``held`` and ``cap`` on ``ladder``; None when either is not on it."""
    if held not in ladder or cap not in ladder:
        return None
    return held if ladder.index(held) <= ladder.index(cap) else cap


def scoped_grant(user: Principal, levels, role: str | None, max_level: str | None) -> tuple:
    """(role, max level) of the grant ``user`` holds, as ``user`` may use it: capped by a run's scope, when it has
    one, to the lower role and the lower level (on ``levels``, the project's ladder) of the grant and the scope."""
    if user.scope is None or role is None:
        return role, max_level
    return lower(ROLES, role, user.scope.role), lower(levels, max_level, user.scope.max_level)


async def project_access(conn: AsyncConnection, user: Principal, name: str) -> ProjectAccess:
    """``user``'s access to project ``name``; 404 when it is not registered, or when ``user`` holds no grant on it
    and is no hub admin, so a name tells nothing about the projects one cannot see. The agent of a run sees the run's
    project alone, with the grant ``scoped_grant`` leaves it."""
    if not user.reaches(name):
        raise HTTPException(404, not_found(name))
    row = await repository.access_row(conn, user.user_id, name)
    if row is None or (row.role is None and not user.admin):
        raise HTTPException(404, not_found(name))
    role, max_level = scoped_grant(user, row.levels, row.role, row.max_level)
    sinks = row.sinks
    rules = ProjectRules(name, row.levels, row.locations, sinks, row.default_label)
    return ProjectAccess(row.id, rules, role, max_level)


async def access_of(conn: AsyncConnection, user_id: int | None, name: str) -> ProjectAccess | None:
    """The access user ``user_id`` holds to project ``name`` now, as its grant says, with no run's scope; None when the
    project is not registered. Without a user (or a grant) the role and max level are None: the project's rules alone."""
    row = await repository.access_row(conn, user_id, name)
    if row is None:
        return None
    rules = ProjectRules(name, row.levels, row.locations, row.sinks, row.default_label)
    return ProjectAccess(row.id, rules, row.role, row.max_level)
