"""The entities step's requests and answers (``evo_agents.hub.server.kg_entities``): the latest requests, and keeping
a run's answers and queueing the build that reads them, in one transaction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_entity_relay as relay
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.server import project_archive
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.service.graph import queue
from evo_agents.hub.server.projects import ProjectAccess


async def latest_requests(engine: AsyncEngine, project_id: int):
    """``relay.latest_requests`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await relay.latest_requests(conn, project_id)


async def keep_answers(
    engine: AsyncEngine,
    jobs,
    store: BlobStore,
    access: ProjectAccess,
    project: str,
    user,
    sha: str,
    path: Path,
    size: int,
) -> bool:
    """Hold the answers uploaded as blob ``sha`` (``path``, ``size`` bytes) as the project's and queue a build; whether
    one was queued. Raises BlobStoreUnavailable."""
    async with UnitOfWork(engine) as conn:
        await relay.hold(conn, store, access.project_id, sha, path, size, relay.ANSWERS_KIND, user.user_id)
        await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
        _, queued = await queue(jobs, conn, access.project_id, project, user.user_id)
    return queued
