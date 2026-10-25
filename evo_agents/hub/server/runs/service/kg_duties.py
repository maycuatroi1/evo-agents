"""The projects whose knowledge graph a worker keeps fresh (feature kg_sync of its daemon): those it serves whose night
shift names it and whose graph is on (feature kg). The heartbeat answer lists them (``kg_duties``) to a daemon that says
kg_sync, and the routes a worker pushes and reads a project's graph through (``evo_agents.hub.server.kg_worker``) take
only a worker on duty for that project (``duty_refusal``): a worker never writes to the graph of a project it does not
serve, nor to one whose night shift another worker keeps. A night shift on a pool (plan worker-pools, criterion a4) is
kept by one node of it (``keeper``): its live node of the lowest id that serves the project and is online, so another
node takes it over when that one goes offline or leaves."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.runs.models import KgDuty

NIGHT_SHIFT = "night_shift"


def keeper(s):
    """The id of the worker that keeps the knowledge graph of schedule row ``s``'s project fresh: its worker, or the
    live node of its pool of the lowest id that serves the project and is online (NULL for none)."""
    w, wp = tables.workers, tables.worker_projects
    fresh = w.c.last_heartbeat_at > func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS)
    node = select(func.min(w.c.id)).where(
        w.c.pool_id == s.c.pool_id,
        w.c.revoked_at.is_(None),
        exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == s.c.project_id),
        fresh,
    )
    return func.coalesce(s.c.worker_id, node.scalar_subquery())


def kept_by(s, worker_id):
    """The condition on schedule row ``s`` that worker ``worker_id`` keeps its project's graph."""
    return or_(s.c.worker_id == worker_id, and_(s.c.pool_id.is_not(None), keeper(s) == worker_id))


async def keeper_of(conn: AsyncConnection, schedule_id: int) -> int | None:
    """``keeper`` of schedule ``schedule_id``."""
    s = tables.schedules
    return (await conn.execute(select(keeper(s)).where(s.c.id == schedule_id))).scalar_one_or_none()


def _duties(worker_id: int):
    """The projects worker ``worker_id`` is on duty for, by name, each with when the hub last took a run of its graph."""
    s, wp, p, i = tables.schedules, tables.worker_projects, tables.projects, tables.kg_ingests
    received = select(func.max(i.c.received_at)).where(i.c.project_id == s.c.project_id).scalar_subquery()
    serves = exists().where(wp.c.worker_id == worker_id, wp.c.project_id == s.c.project_id)
    return (
        select(p.c.name.label("project"), received.label("received_at"))
        .join_from(s, p, p.c.id == s.c.project_id)
        .where(
            kept_by(s, worker_id),
            s.c.kind == NIGHT_SHIFT,
            serves,
            hub_settings.feature_on("kg", s.c.project_id),
        )
        .order_by(p.c.name)
    )


async def kg_duties(conn: AsyncConnection, worker_id: int) -> list[KgDuty]:
    """What the heartbeat answer of worker ``worker_id`` lists in kg_sync."""
    return [KgDuty(**row._mapping) for row in await conn.execute(_duties(worker_id))]


async def duty_refusal(conn: AsyncConnection, worker_id: int, worker: str, project: str) -> str | None:
    """Why worker ``worker_id`` (named ``worker``) may not push to nor read the graph of ``project``; None when it is
    on duty for it. A project it does not serve is named as such, whatever else holds."""
    p, wp, s = tables.projects, tables.worker_projects, tables.schedules
    row = (
        await conn.execute(
            select(
                exists().where(wp.c.worker_id == worker_id, wp.c.project_id == p.c.id).label("serves"),
                exists()
                .where(s.c.project_id == p.c.id, s.c.kind == NIGHT_SHIFT, kept_by(s, worker_id))
                .label("on_duty"),
            ).where(p.c.name == project)
        )
    ).one_or_none()
    if row is None or not row.serves:
        return f"worker {worker} does not serve project {project}: it may not touch its knowledge graph"
    if not row.on_duty:
        return (
            f"worker {worker} is not on duty for the night shift of project {project}: only the worker its charter "
            "names, or one node of the pool it names, keeps its knowledge graph fresh"
        )
    return None
