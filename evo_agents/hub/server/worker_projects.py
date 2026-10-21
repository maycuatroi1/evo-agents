"""The projects a worker serves (``tables.worker_projects``): who may name them, and how its owner changes them in place
with PATCH /v1/workers/{id} (``evo_agents.hub.server.workers``).

A worker serves the projects of its pairing or registration, each one its owner held the writer role on then
(``writable``: 404 for a project they cannot see, 403 below writer). Its owner changes them in place by giving the whole
list (``serve``), in the transaction of the PATCH that holds the worker's row locked, which the claims of its daemon
lock too: a project added needs the writer role again; one it serves already stays whatever role its owner holds on it
now, as a worker keeps serving a project after a grant changes and claims skip that project's runs meanwhile; and
dropping one is 409, naming why and changing nothing, while a run of that project in an active state
(``runs.ACTIVE_STATES``) is held by the worker or pinned to it, or while the charter of that project's Curator puts its
night shift on duty there (a schedule ``night_shift``) (``still_needed``). The worker keeps its id, its token, its
other settings and its secret bindings, which name the worker and not a project. Its daemon learns the list from
``settings.projects`` of its next heartbeat answer.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import delete, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal

NIGHT_SHIFT = "night_shift"  # the kind of schedule the charter of a project's Curator keeps on one worker


async def writable(conn, user: Principal, names: list[str]) -> list[int]:
    """The ids of the projects ``names``; 404 for one ``user`` cannot see, 403 for one without the writer role."""
    ids = []
    for name in names:
        access = await project_access(conn, user, name)
        if not has_role(access.role, "writer"):
            role = access.role or "no role"
            raise HTTPException(403, f"a worker of project {name} needs the writer role on it; you hold {role}")
        ids.append(access.project_id)
    return ids


async def served(conn: AsyncConnection, worker_id: int) -> dict[str, int]:
    """The projects the worker serves, name -> id, by name."""
    wp, projects = tables.worker_projects, tables.projects
    names = (
        select(projects.c.name, projects.c.id)
        .join_from(wp, projects, projects.c.id == wp.c.project_id)
        .where(wp.c.worker_id == worker_id)
        .order_by(projects.c.name)
    )
    return dict((await conn.execute(names)).all())


async def still_needed(conn: AsyncConnection, worker_id: int, name: str, dropped: dict[str, int]) -> None:
    """409 naming why, when a project of ``dropped`` (name -> id) still needs the worker: a run of it that is active and
    held by or pinned to the worker, or its night shift, which the charter of its Curator puts on duty there."""
    if not dropped:
        return
    r, s = tables.runs, tables.schedules
    by_id = {project_id: project for project, project_id in dropped.items()}
    active = (
        select(r.c.project_id, r.c.id)
        .where(
            r.c.project_id.in_(list(by_id)),
            r.c.state.in_(runs.ACTIVE_STATES),
            or_(r.c.worker_id == worker_id, r.c.pinned_worker_id == worker_id),
        )
        .order_by(r.c.id)
    )
    held: dict[str, list[str]] = {}
    for project_id, run_id in await conn.execute(active):
        held.setdefault(by_id[project_id], []).append(str(run_id))
    duty = select(s.c.project_id).where(
        s.c.project_id.in_(list(by_id)), s.c.kind == NIGHT_SHIFT, s.c.worker_id == worker_id
    )
    on_duty = {by_id[project_id] for project_id in (await conn.execute(duty)).scalars()}
    why = [
        f"{project} has active runs held by it or pinned to it ({', '.join(held[project])}): let them end or cancel "
        "them"
        for project in sorted(held)
    ]
    why += [
        f"the Curator's charter of {project} puts its night shift on duty there: name another worker in the charter "
        f"(`evo-agents hub curator charter`)"
        for project in sorted(on_duty)
    ]
    if why:
        raise HTTPException(
            409,
            f"worker {name} cannot stop serving {', '.join(sorted({*held, *on_duty}))}: "
            f"{'; '.join(why)}; nothing was changed",
        )


async def serve(
    conn: AsyncConnection, user: Principal, worker_id: int, name: str, served: dict[str, int], projects: list[str]
) -> None:
    """Make the worker serve exactly ``projects``, in the caller's transaction. Each project added needs the writer
    role (``writable``: 404 for one ``user`` cannot see, 403 below writer); one it serves already stays whatever role
    its owner holds on it now, as a worker keeps serving a project after a grant changes; one dropped must not need
    the worker any longer (``still_needed``)."""
    added = [project for project in projects if project not in served]
    dropped = {project: project_id for project, project_id in served.items() if project not in projects}
    added_ids = await writable(conn, user, added)
    await still_needed(conn, worker_id, name, dropped)
    wp = tables.worker_projects
    if dropped:
        await conn.execute(delete(wp).where(wp.c.worker_id == worker_id, wp.c.project_id.in_(list(dropped.values()))))
    if added_ids:
        await conn.execute(insert(wp), [{"worker_id": worker_id, "project_id": project_id} for project_id in added_ids])
