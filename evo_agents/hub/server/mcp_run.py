"""The agent of a run on /mcp (``evo_agents.hub.server.mcp``): the run a worker token names in X-Evo-Run, which that
worker must hold now, and the principal its agent acts as, the run's owner within the run's scope."""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import question_runs, tables
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
    login, and the project's ladder and default label. That is the worker's owner, or another writer of the project on a shared worker (``docs/workers.md``)."""
    workers, runs, projects, grants, users = tables.workers, tables.runs, tables.projects, tables.grants, tables.users
    return (
        select(
            runs.c.state,
            projects.c.name,
            grants.c.role,
            grants.c.max_level,
            runs.c.kind,
            runs.c.dispatched_by,
            users.c.login,
            projects.c.levels,
            projects.c.default_label,
        )
        .select_from(
            workers.join(runs, runs.c.worker_id == workers.c.id)
            .join(projects, projects.c.id == runs.c.project_id)
            .join(users, users.c.id == runs.c.dispatched_by)
            .outerjoin(grants, and_(grants.c.project_id == runs.c.project_id, grants.c.user_id == runs.c.dispatched_by))
        )
        .where(workers.c.token_id == token_id, workers.c.revoked_at.is_(None), runs.c.id == run_id)
    )


async def run_principal(conn: AsyncConnection, user: Principal, run_id: int) -> Principal:
    """The agent of run ``run_id`` for the worker whose token ``user`` authenticated: the run's owner, the member who
    dispatched it, never a hub admin, within the run's scope (its project, the owner's grant there with its role capped
    at writer; a question run's reader, up to the level of the project's default label). On a shared worker the owner
    of the run may be another member than the worker's. HTTPException 403 when that worker does not hold the run now
    (another worker's, one that ended, none) or the run's owner holds no grant on the project any more."""
    row = (await conn.execute(_held_run(user.token_id, run_id))).first()
    if row is None or row.state not in HELD_STATES:
        raise HTTPException(403, NOT_HELD.format(run=run_id))
    project, role, max_level, kind = row.name, row.role, row.max_level, row.kind
    if role is None:
        raise HTTPException(403, f"the owner of run {run_id} holds no grant on project {project} any more")
    role = "writer" if has_role(role, "writer") else role
    if kind == "question":  # its answer leaves the hub: it reads, up to the label the answer carries
        role, max_level = "reader", question_runs.lowered_level(row.levels, max_level, row.default_label)
    scope = RunScope(run_id, project, role, max_level, kind)
    return replace(user, user_id=row.dispatched_by, login=row.login, admin=False, scope=scope)
