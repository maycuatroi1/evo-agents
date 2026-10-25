"""A caller's access to a project, for the routes, tools and jobs of a core plugin (API 1.3): the same check the hub's
own routes make, and the member a run of the plugin acts for. Private to ``core_plugin_api``."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, tables
from evo_agents.hub.server import projects
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.core_plugin_api._types import (
    ChatRunRef,
    Plugin,
    ProjectRef,
    Refused,
    Session,
    issue_session,
    own_plugin,
    own_session,
    refusing,
    session_principal,
)
from evo_agents.hub.server.security import MACHINE, Principal

ROLES: tuple[str, ...] = access.ROLES  # each role may do what the ones before it may
NEEDS_ROLE = "{login} is {held} of project {project}: this needs {needed}"


def has_role(held: str | None, needed: str) -> bool:
    """Whether role ``held`` (None: no grant) may do what ``needed`` may."""
    if needed not in ROLES:
        raise ValueError(f"a role is one of {', '.join(ROLES)}")
    return access.has_role(held, needed)


async def project_access(conn: AsyncConnection, caller: Session, project: str, *, role: str = "reader") -> ProjectRef:
    """``caller``'s access to ``project``, as the hub's routes check it: 404 when the project does not exist or the
    caller holds no grant on it and is no hub admin (the same words, which tell nothing of the projects one cannot
    see), 403 when their role is below ``role``. A hub admin without a grant passes as admin."""
    own_session(caller)
    if role not in ROLES:
        raise ValueError(f"a role is one of {', '.join(ROLES)}")
    principal = session_principal(caller)
    with refusing():
        found = await projects.project_access(conn, principal, project)
    held = found.role if found.role is not None or not principal.admin else "admin"
    if not access.has_role(held, role):
        article = f"a {held}" if held else "no member"
        raise Refused(403, NEEDS_ROLE.format(login=caller.login, held=article, project=project, needed=role))
    return ProjectRef(found.project_id, found.name, held, found.max_level)


async def chat_run_of(conn: AsyncConnection, plugin: Plugin, run_id: int | None) -> ChatRunRef | None:
    """The chat run ``run_id`` (``Context.run_id`` of a tool the agent of a run calls), or None for a run of another
    kind or no run."""
    own_plugin(plugin)
    if run_id is None:
        return None
    found = await chat_run(conn, run_id)
    if found is None:
        return None
    return ChatRunRef(
        found.run_id, found.chat_id, found.root_id, found.profile_id, found.dispatcher_id, found.dispatcher
    )


async def run_session(conn: AsyncConnection, plugin: Plugin, run_id: int | None) -> Session | None:
    """The member chat run ``run_id`` acts for, the person at the origin of its chain, as a ``Session`` (``machine``,
    no token of its own) that ``project_access`` checks as the hub checks that member; None for a run of another kind
    or no run. A hub admin among them is no admin here."""
    found = await chat_run_of(conn, plugin, run_id)
    if found is None:
        return None
    u = tables.users
    login = (await conn.execute(select(u.c.login).where(u.c.id == found.dispatcher_id))).scalar_one_or_none()
    if login is None:
        return None
    principal = Principal(found.dispatcher_id, login, False, None, MACHINE, "")
    return issue_session(found.dispatcher_id, login, 0, False, principal=principal)
