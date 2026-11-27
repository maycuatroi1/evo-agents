"""Whom a call to the records API is for (``docs/plugins.md``, section Records).

A backend touches the records of a project only on behalf of the caller of the call it is serving, within that
caller's grant, or, for a job or an event with no caller, within the levels the admin of the project granted it when
enabling the plugin (``project_plugins.records_level``).

- The delegation. When the hub hands a backend a call whose caller holds a grant on the project, and the project granted
  the plugin ``records:use``, it mints a lease: a random token ``evd_...`` whose SHA-256 the hub stores
  (``plugin_record_leases``) with the plugin, the project, the caller, the run and sink the call came through, and the
  highest level the caller reached then. The token goes to the backend as ``caller.token`` of the call. The backend
  cannot make one: only the hub holds the table, and a token is checked against the plugin and the project of the
  request, so another plugin's, or another project's, or an expired one opens nothing. It lives for the call's time
  and a minute.
- The use. A backend sends it as ``X-Evo-On-Behalf-Of`` along with its own plugin token. The hub then reads the
  caller's grant again, so a grant revoked or lowered since is honoured: the call reaches the lower of the lease's
  level and the grant's. A header that does not check out is a 403, never a call with no caller.
- No header is a call with no caller: the records it reaches are those up to ``records_level``, none when the admin set
  none, and its audience filter does not apply (nobody is its reader). A call with no caller cannot name its writer as a
  record's audience.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls
from evo_agents.hub.access import has_role
from evo_agents.hub.plugin_records import Reach
from evo_agents.hub.server.chats.service.prompts import run_clearers
from evo_agents.hub.server.plugins.models.delegation import (
    AGENT_SINK,
    CHAT_RUN,
    HEADER,
    PERMISSION,
    PREFIX,
    SLACK,
    Resolved,
)
from evo_agents.hub.server.plugins.repository import delegation as repository
from evo_agents.hub.server.plugins.repository.enabled import enabled_in
from evo_agents.hub.server.projects import ProjectAccess, access_of
from evo_agents.hub.server.security import WEB, Principal, hash_token


def _lower(ladder, held, cap):
    """The lower of ``held`` and ``cap`` on ``ladder``; None when either is not on it."""
    if held not in ladder or cap not in ladder:
        return None
    return held if ladder.index(held) <= ladder.index(cap) else cap


def through(user: Principal, sink: str | None) -> str | None:
    """The sink a principal reads through: the one named; none for a web session (the member reads by their grant), else
    Claude Code's."""
    if sink is not None:
        return sink
    return None if user.kind == WEB else AGENT_SINK


async def people_of(conn: AsyncConnection, user_id: int, run_id: int | None, run_kind: str | None) -> frozenset[int]:
    """Who a read must be cleared for: both people of a chat run, else the person."""
    if run_id is not None and run_kind == CHAT_RUN:
        return frozenset(await run_clearers(conn, run_id) or [user_id])
    return frozenset({user_id})


async def mint(
    conn: AsyncConnection, plugin_id: int, access: ProjectAccess, user: Principal, sink: str | None, ttl: float
) -> str:
    """A delegation for one call of plugin ``plugin_id`` on behalf of ``user`` in the project of ``access``."""
    token = PREFIX + secrets.token_urlsafe(32)
    scope = user.scope
    values = {
        "token_hash": hash_token(token),
        "plugin_id": plugin_id,
        "project_id": access.project_id,
        "user_id": user.user_id,
        "run_id": scope.run_id if scope else None,
        "run_kind": scope.kind if scope else None,
        "sink": through(user, sink),
        "max_level": access.max_level,
    }
    await repository.add_lease(conn, values, timedelta(seconds=ttl) + SLACK)
    return token


def _refused(message: str) -> HTTPException:
    return HTTPException(403, message)


async def _enabled(conn: AsyncConnection, plugin_id: int, project: str):
    row = await enabled_in(conn, plugin_id, project)
    if row is None or PERMISSION not in plugin_calls.effective_grant(row):
        raise _refused(
            f"the plugin is not enabled in project {project} with {PERMISSION} granted: an admin of the project "
            "enables it and grants what it asks for"
        )
    return row


async def on_behalf(conn: AsyncConnection, request: Request, plugin_id: int, project: str) -> Resolved:
    """What a call to the records API from plugin ``plugin_id`` reaches in ``project``: on behalf of the caller its
    ``X-Evo-On-Behalf-Of`` delegation names, else, with no header, the levels up to ``records_level``."""
    row = await _enabled(conn, plugin_id, project)
    given = request.headers.get(HEADER)
    if given is not None:
        return await _leased(conn, given, plugin_id, row)
    level = await repository.records_level(conn, row.project_id, plugin_id)
    if level is None:
        raise _refused(
            f"this call has no caller and project {project} granted plugin no levels for such calls: send the "
            f"{HEADER} of the call you serve, or an admin of the project sets records_level when enabling the plugin"
        )
    access = await access_of(conn, None, project)
    reach = Reach(row.project_id, plugin_id, access.rules, level)  # pyright: ignore[reportOptionalMemberAccess]
    return Resolved(reach, "writer")


async def _leased(conn: AsyncConnection, given: str, plugin_id: int, row) -> Resolved:
    found = None
    if given.startswith(PREFIX):
        found = await repository.live_lease(conn, hash_token(given), plugin_id, row.project_id)
    if found is None:
        raise _refused(f"{HEADER} is not a live delegation of the hub for this plugin and project")
    access = await access_of(conn, found.user_id, row.project)
    if access is None or access.role is None:
        raise _refused("the person this call is for holds no grant on the project now")
    level = _lower(access.rules.levels, access.max_level, found.max_level)
    people = await people_of(conn, found.user_id, found.run_id, found.run_kind)
    reach = Reach(row.project_id, plugin_id, access.rules, level, found.user_id, found.sink, people)
    return Resolved(reach, access.role)


def may_write(resolved: Resolved) -> None:
    if not has_role(resolved.role, "writer"):
        raise HTTPException(403, "writing a plugin's records needs the writer role on the project")
