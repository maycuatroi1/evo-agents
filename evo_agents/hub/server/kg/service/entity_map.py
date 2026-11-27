"""Keeping the entity map a run posts (``evo_agents.hub.server.kg_entity_map``) and queueing the build that reads
it, in one transaction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_entity_map_db
from evo_agents.hub import kg_entity_maps as maps
from evo_agents.hub import kg_entity_relay as relay
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.server import project_archive
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.service.graph import queue
from evo_agents.hub.server.projects import ProjectAccess


async def keep_map(
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
    """Hold the map uploaded as blob ``sha`` (``path``, ``size`` bytes) as the project's, count it on the run's entity
    map run, if any, and queue a build; whether one was queued. Raises BlobStoreUnavailable."""
    async with UnitOfWork(engine) as conn:
        await relay.hold(conn, store, access.project_id, sha, path, size, maps.MAP_KIND, user.user_id)
        await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
        await kg_entity_map_db.posted(conn, user.acts_for_run)  # the entity map run it belongs to, if any
        _, queued = await queue(jobs, conn, access.project_id, project, user.user_id)
    return queued
