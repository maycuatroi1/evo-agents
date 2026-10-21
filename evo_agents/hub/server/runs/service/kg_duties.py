"""The projects whose knowledge graph a worker keeps fresh (feature kg_sync of its daemon): those it serves whose night
shift names it and whose graph is on (feature kg). The heartbeat answer lists them (``kg_duties``) to a daemon that says
kg_sync, and the routes a worker pushes and reads a project's graph through (``evo_agents.hub.server.kg_worker``) take
only a worker on duty for that project (``duty_refusal``): a worker never writes to the graph of a project it does not
serve, nor to one whose night shift another worker keeps."""

from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.runs.models import KgDuty

NIGHT_SHIFT = "night_shift"


def _duties(worker_id: int):
    """The projects worker ``worker_id`` is on duty for, by name, each with when the hub last took a run of its graph."""
    s, wp, p, i = tables.schedules, tables.worker_projects, tables.projects, tables.kg_ingests
    received = select(func.max(i.c.received_at)).where(i.c.project_id == s.c.project_id).scalar_subquery()
    serves = exists().where(wp.c.worker_id == worker_id, wp.c.project_id == s.c.project_id)
    return (
        select(p.c.name.label("project"), received.label("received_at"))
        .join_from(s, p, p.c.id == s.c.project_id)
        .where(
            s.c.worker_id == worker_id,
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
                .where(s.c.project_id == p.c.id, s.c.kind == NIGHT_SHIFT, s.c.worker_id == worker_id)
                .label("on_duty"),
            ).where(p.c.name == project)
        )
    ).one_or_none()
    if row is None or not row.serves:
        return f"worker {worker} does not serve project {project}: it may not touch its knowledge graph"
    if not row.on_duty:
        return (
            f"worker {worker} is not on duty for the night shift of project {project}: only the worker its charter "
            "names keeps its knowledge graph fresh"
        )
    return None
