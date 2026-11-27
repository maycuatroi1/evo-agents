"""The graph routes of the worker on duty for a project's night shift (``evo_agents.hub.server.kg_worker``): whether
the worker whose token made a request is on duty, and what its sync reads of the runs the hub holds."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.repository import worker as repository
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.kg_duties import duty_refusal

log = logging.getLogger("evo_agents.hub.server.kg_worker")


async def on_duty(engine: AsyncEngine, user, project: str) -> None:
    """403 unless the worker whose token made the request is on duty for ``project``."""
    async with UnitOfWork(engine) as conn:
        worker = await worker_of(conn, user)
        refusal = await duty_refusal(conn, worker.id, worker.name, project)
    if refusal is not None:
        log.warning("worker kg request refused", extra={"project": project, "worker_id": worker.id})
        raise HTTPException(403, refusal)


async def runs(engine: AsyncEngine, user, project: str, source: str | None) -> tuple[list[str], list[str]]:
    """(ingested, pending): the ids of the runs the hub holds of the project, or of one of its sources."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        ingested = await repository.ingested(conn, access.project_id, source)
        held = await repository.pending(conn, access.project_id, source)
    return ingested, held


async def run_log(engine: AsyncEngine, user, project: str, run_id):
    """(source, log_sha256, log_size) of an ingested run of the project; 404 for none."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        row = await repository.run_log(conn, access.project_id, run_id)
    if row is None:
        raise HTTPException(404, f"project {project} has no ingested run {run_id}")
    return row
