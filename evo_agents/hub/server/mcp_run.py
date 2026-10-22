"""The agent of a run on /mcp (``evo_agents.hub.server.mcp``): the run a worker token names in X-Evo-Run, which that
worker must hold now, and the principal its agent acts as, the run's owner within the run's scope."""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy import and_, case, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.runs import HELD_STATES
from evo_agents.hub.server.security import Principal, RunScope

MOUNT = "/mcp"  # where the hub mounts its MCP server (``evo_agents.hub.server.mcp``)
NOT_HELD = (
    f"this worker does not hold run {{run}} now: it ended, or another worker holds it, and {MOUNT} opens for the "
    "agent of a run its worker holds only"
)


def _held_run(token_id: int, run_id: int):
    """The run ``run_id`` that the live worker of token ``token_id`` holds: its state, its project, the grant of its
    owner there (role and max_level None without one), its kind, and its owner, the member who dispatched it, by id and
    login. That is the worker's owner, or another writer of the project on a shared worker (``docs/workers.md``). Then
    what its memories need: its profile, version, the handle of a profile that is not its project's default, its hop,
    chat and chat message."""
    workers, runs, projects, grants, users = tables.workers, tables.runs, tables.projects, tables.grants, tables.users
    profiles = tables.agent_profiles
    return (
        select(
            runs.c.state,
            projects.c.name,
            grants.c.role,
            grants.c.max_level,
            runs.c.kind,
            runs.c.dispatched_by,
            users.c.login,
            runs.c.profile_id,
            runs.c.profile_version,
            case((profiles.c.is_default, None), else_=profiles.c.handle).label("agent"),
            runs.c.hop,
            runs.c.chat_id,
            runs.c.chat_message_id,
        )
        .select_from(
            workers.join(runs, runs.c.worker_id == workers.c.id)
            .join(projects, projects.c.id == runs.c.project_id)
            .join(users, users.c.id == runs.c.dispatched_by)
            .outerjoin(grants, and_(grants.c.project_id == runs.c.project_id, grants.c.user_id == runs.c.dispatched_by))
            .outerjoin(profiles, profiles.c.id == runs.c.profile_id)
        )
        .where(workers.c.token_id == token_id, workers.c.revoked_at.is_(None), runs.c.id == run_id)
    )


async def run_principal(conn: AsyncConnection, user: Principal, run_id: int) -> Principal:
    """The agent of run ``run_id`` for the worker whose token ``user`` authenticated: the run's owner, the member who
    dispatched it, never a hub admin, within the run's scope (its project, the owner's grant there with its role capped
    at writer). On a shared worker the owner of the run may be another member than the worker's. HTTPException 403 when
    that worker does not hold the run now (another worker's, one that ended, none) or the run's owner holds no grant on
    the project any more."""
    row = (await conn.execute(_held_run(user.token_id, run_id))).first()
    if row is None or row.state not in HELD_STATES:
        raise HTTPException(403, NOT_HELD.format(run=run_id))
    _, project, role, max_level, kind, owner_id, owner, *memory = row
    if role is None:
        raise HTTPException(403, f"the owner of run {run_id} holds no grant on project {project} any more")
    scope = RunScope(run_id, project, "writer" if has_role(role, "writer") else role, max_level, kind, *memory)
    return replace(user, user_id=owner_id, login=owner, admin=False, scope=scope)
