"""Who may push to, build and read a project's knowledge graph, and the transactions of its runs and builds
(``evo_agents.hub.server.kg``): each public function that takes the engine opens one, as its route did."""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.access import Refused
from evo_agents.hub.kg_build import QueueBusy, queue_build
from evo_agents.hub.kg_ingest import RunLog
from evo_agents.hub.kg_source_state import SourceState, source_state
from evo_agents.hub.server import audit, project_archive
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.graph import Build, Builds, MissingBlobs, Queued, RunCommitted, Runs, RunState
from evo_agents.hub.server.kg.repository import graph as repository
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import holds
from evo_agents.kg.policy import Label

log = logging.getLogger("evo_agents.hub.server.kg")

# Access


def member(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the knowledge graph of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, doing: str) -> None:
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"{doing} project {access.name} needs the writer role on it")


def hub_ceiling(access: ProjectAccess) -> Label:
    """The writer role and a usable hub sink, as for any push; the label the hub sink clears."""
    rules = access.rules
    try:
        rules.check_push({"level": rules.levels[0]}, access.role)
    except Refused as exc:
        raise HTTPException(exc.status, str(exc)) from None
    assert rules.hub_sink is not None  # check_push refuses a project without one
    return rules.clearance_label(rules.hub_sink)  # the highest audience: an item with one may be pushed


async def writer_access(engine: AsyncEngine, user, project: str, doing: str) -> ProjectAccess:
    """The caller's access to ``project``, which must hold the writer role (``writer``), in a transaction of its
    own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        writer(access, doing)
    return access


async def config(conn: AsyncConnection, access: ProjectAccess) -> dict:
    knowledge = await repository.knowledge(conn, access.project_id)
    if knowledge is None:
        raise HTTPException(
            422,
            f"project {access.name} has no knowledge config on the hub yet: `evo-agents hub kg push` sends it first",
        )
    return knowledge


# Runs


async def list_runs(engine: AsyncEngine, user, project: str) -> Runs:
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        ingested = await repository.run_ids(conn, tables.kg_ingests, access.project_id)
        pending = await repository.run_ids(conn, tables.kg_pending_runs, access.project_id)
    return Runs(ingested=ingested, pending=pending)


async def read_source_state(
    engine: AsyncEngine, user, project: str, source: str, scope: str | None, *, items: bool
) -> SourceState:
    """What the hub holds of one source (``kg_source_state``): the writer role, as for a push."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        writer(access, "syncing a source of")
        return await source_state(conn, access.project_id, source, scope, items=items)


async def check_blobs(engine: AsyncEngine, user, project: str, hashes: list[str]) -> MissingBlobs:
    """The hashes of ``hashes`` the project does not hold."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        writer(access, "pushing to")
        return MissingBlobs(missing=await repository.missing(conn, access.project_id, hashes))


async def missing_blobs(engine: AsyncEngine, project_id: int, hashes) -> list[str]:
    """``repository.missing`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await repository.missing(conn, project_id, hashes)


async def push_access(engine: AsyncEngine, user, project: str, run_id: uuid.UUID):
    """(the access, the hub sink's label, the knowledge config, the run's ingested and pending logs) of a push of run
    ``run_id``: the checks of ``hub_ceiling`` on a project that is not archived and has a config."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await project_archive.refuse_archived(conn, access.project_id, "it takes no kg push")
        ceiling = hub_ceiling(access)
        knowledge = await config(conn, access)
        ingested, pending = await repository.held_logs(conn, access.project_id, run_id)
    return access, ceiling, knowledge, ingested, pending


async def drop_upload(engine: AsyncEngine, user, project_id: int, upload_id: uuid.UUID) -> str | None:
    """Delete the row of an upload the caller asked for and no longer needs; its declared SHA-256."""
    async with UnitOfWork(engine) as conn:
        return await repository.delete_upload(conn, project_id, upload_id, user.user_id)


async def log_size(engine: AsyncEngine, project_id: int, sha256: str) -> int | None:
    """The size of the project's blob ``sha256``, the log a push names instead of an upload; None when it holds none."""
    async with UnitOfWork(engine) as conn:
        return await repository.blob_size(conn, project_id, sha256)


async def keep_pending(engine: AsyncEngine, user, project: str, project_id: int, run_id: uuid.UUID, found: dict):
    """Keep the checked log in ``found`` as a pending run; its state, with the blobs the project still lacks."""
    run: RunLog = found["run"]
    blobs = sorted(run.blobs)
    async with UnitOfWork(engine) as conn:
        await repository.add_pending(
            conn,
            project_id=project_id,
            run_id=run_id,
            source=run.source,
            log_sha256=found["sha256"],
            log_size=found["size"],
            blobs=blobs,
            pushed_by=user.user_id,
            **run.facts.columns(),
        )
        missing = await repository.missing(conn, project_id, blobs)
    log.info(
        "kg run log accepted",
        extra={
            "project": project,
            "login": user.login,
            "items": run.items,
            "blobs": len(blobs),
            "missing": len(missing),
        },
    )
    return RunState(run_id=str(run_id), status="pending", log_sha256=found["sha256"], blobs=len(blobs), missing=missing)


# Builds


async def queue(jobs, conn: AsyncConnection, project_id: int, project: str, requested_by: int | None):
    """(the build queued or waiting, whether one was queued) through the app's ``jobs``; 503 when the queue is busy."""
    try:
        return await queue_build(jobs, conn, project_id, project, requested_by)
    except QueueBusy as exc:
        raise HTTPException(503, str(exc)) from None


async def commit_run(engine: AsyncEngine, jobs, user, project: str, run_id: uuid.UUID) -> RunCommitted | list[str]:
    """Make a pushed run part of the project's corpus, once every blob it refers to is there, and queue a build; the
    blobs it refers to that the project lacks instead, when there are any, and then nothing is written."""
    run = str(run_id)
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        hub_ceiling(access)
        pending = await repository.locked_pending(conn, access.project_id, run_id)
        if pending is None:
            if await repository.ingested(conn, access.project_id, run_id):
                return RunCommitted(run_id=run, status="ingested", created=False, build=None, queued=False)
            raise HTTPException(
                422, f"run {run} has no checked log on the hub: POST /v1/kg/{project}/runs with its log first"
            )
        source, log_sha256, log_size, blobs = pending[:4]
        columns = repository.facts(tables.kg_pending_runs.c)
        facts = {column.name: value for column, value in zip(columns, pending[4:], strict=True)}
        missing = await repository.missing(conn, access.project_id, blobs)
        if missing:
            return missing
        await repository.ingest(
            conn,
            access.project_id,
            run_id,
            source=source,
            log_sha256=log_sha256,
            log_size=log_size,
            pushed_by=user.user_id,
            **facts,
        )
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_INGEST, target=project)
        build_id, queued = await queue(jobs, conn, access.project_id, project, None)
        build = await _build(conn, access.project_id, build_id)
    log.info(
        "kg run ingested",
        extra={"project": project, "login": user.login, "blobs": len(blobs), "build_id": build_id, "queued": queued},
    )
    return RunCommitted(run_id=run, status="ingested", created=True, build=build, queued=queued)


async def _build(conn: AsyncConnection, project_id: int, build_id: int | None) -> Build | None:
    return (await repository.find_builds(conn, project_id, build_id=build_id))[0] if build_id is not None else None


async def list_builds(engine: AsyncEngine, user, project: str, *, status: str | None, limit: int) -> Builds:
    """The project's builds, newest first (only those with ``status`` when given), and its build jobs still in the
    queue."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        builds = await repository.find_builds(conn, access.project_id, status=status, limit=limit)
        jobs = await repository.queued_jobs(conn, project)
    return Builds(builds=builds, jobs=jobs)


async def show_build(engine: AsyncEngine, user, project: str, build_id: int) -> Build:
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        found = await repository.find_builds(conn, access.project_id, build_id=build_id, limit=1)
    if not found:
        raise HTTPException(404, f"project {project} has no build {build_id}")
    return found[0]


async def request_build(engine: AsyncEngine, jobs, user, project: str) -> Queued:
    """Queue a build of the project's graph from the runs the hub holds."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        writer(access, "building")
        await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
        build_id, queued = await queue(jobs, conn, access.project_id, project, user.user_id)
        if queued:
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_BUILD, target=project
            )
        build = await _build(conn, access.project_id, build_id)
    log.info("kg build requested", extra={"project": project, "login": user.login, "queued": queued})
    return Queued(build=build, queued=queued)
