"""The priority of a run (runs.priority, one of ``runs.PRIORITIES``, ``runs.DEFAULT_PRIORITY`` unless set): the order
a worker claims queued runs in, by priority first and then by id (``claim_order``, ``claim_rules._claimable``), how
many queued runs a node takes before a run waiting for its slot (``ahead``), and the change of the priority of a
queued run by a writer of its project (``change_priority``: POST /v1/projects/{p}/runs/{id}/priority, ``evo-agents
hub run priority``), which ``set_priority`` does for the Orchestrator's ops action too. A held run keeps its slot
whatever its priority: only the queue's order changes."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service.audits import audit_run
from evo_agents.hub.server.runs.service.views import run_view
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger

RANKS = {level: rank for rank, level in enumerate(runs.PRIORITIES)}  # urgent 0, then high, medium and low


def rank(column):
    """The rank of the priority in ``column``, 0 for urgent; a value no level names counts as the default."""
    return case(RANKS, value=column, else_=RANKS[runs.DEFAULT_PRIORITY])


def claim_order(r=None) -> tuple:
    """The order a worker claims the queued runs of ``r`` (tables.runs by default) in: priority, then id."""
    r = tables.runs if r is None else r
    return rank(r.c.priority), r.c.id


def before(r, priority: str, run_id: int):
    """Whether the run of ``r`` comes before a queued run of ``priority`` and id ``run_id`` in the claim's order."""
    mine = RANKS.get(priority, RANKS[runs.DEFAULT_PRIORITY])
    return or_(rank(r.c.priority) < mine, and_(rank(r.c.priority) == mine, r.c.id < run_id))


async def ahead(conn: AsyncConnection, run_id: int, worker_id: int) -> int:
    """How many runs queued for worker ``worker_id`` (pinned to it) its claim takes before queued run ``run_id``."""
    r = tables.runs
    priority = (await conn.execute(select(r.c.priority).where(r.c.id == run_id))).scalar_one()
    query = select(func.count()).where(
        r.c.state == "queued", r.c.pinned_worker_id == worker_id, r.c.id != run_id, before(r, priority, run_id)
    )
    return (await conn.execute(query)).scalar_one()


async def set_priority(conn: AsyncConnection, run_id: int, level: str, by: str) -> str:
    """Give queued run ``run_id``, its row locked here, the priority ``level``, which a member or an agent ``by`` set;
    the level it had. 409 for a run that is not queued; its log says who changed it."""
    if level not in RANKS:
        raise HTTPException(422, f"a priority is one of {', '.join(runs.PRIORITIES)}, not {level}")
    r = tables.runs
    lock = select(r.c.state, r.c.priority).where(r.c.id == run_id).with_for_update(of=r)
    row = (await conn.execute(lock)).one_or_none()
    if row is None:
        raise HTTPException(404, f"no run {run_id}")
    if row.state != "queued":
        raise HTTPException(409, f"run {run_id} is {row.state}: only a queued run's priority changes")
    if row.priority != level:
        await conn.execute(update(r).values(priority=level).where(r.c.id == run_id))
        await write_event(conn, run_id, {"text": f"{by} set the priority of the run to {level}, from {row.priority}"})
    return row.priority


async def change_priority(engine, user: Principal, project: str, run_id: int, level: str) -> Run:
    """Give a queued run of ``project`` the priority ``level``, for a writer of the project: 404 for a run of another
    project or one the caller cannot see, 403 below writer, 409 for a run that is not queued."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        r = tables.runs
        found = select(r.c.id).where(r.c.id == run_id, r.c.project_id == access.project_id)
        if (await conn.execute(found)).one_or_none() is None:
            raise HTTPException(404, f"project {access.name} has no run {run_id}")
        if not holds(access.role, "writer"):
            raise HTTPException(403, f"changing the priority of a run of project {access.name} needs the writer role")
        was = await set_priority(conn, run_id, level, user.login)
        if was != level:
            await audit_run(
                conn, user, access, audit.RUN_PRIORITY, f"{access.name} run:{run_id} priority={was}->{level}"
            )
        view = await run_view(conn, run_id)
    log.info("run priority set", extra={"run_id": run_id, "priority": level, "was": was, "login": user.login})
    return view
