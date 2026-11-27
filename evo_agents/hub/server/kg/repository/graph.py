"""The SQL of pushing runs, building and reading a project's knowledge graph: its knowledge config, its pending and
ingested runs, the blobs it holds, its builds and their jobs, and the builds whose graph its tables keep."""

from __future__ import annotations

import uuid

from sqlalchemy import Text, and_, cast, delete, exists, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.jobs import kg_lock
from evo_agents.hub.kg_build import procrastinate_jobs
from evo_agents.hub.kg_graph import BuiltGraph
from evo_agents.hub.kg_graph_rows import MAIN
from evo_agents.hub.kg_sources import reads_chats
from evo_agents.hub.server.kg.models.graph import BUILDS_LIMIT, Build, Job

# Runs


async def knowledge(conn: AsyncConnection, project_id: int) -> dict | None:
    """The knowledge config the project's builds read; None before its first push."""
    configs = tables.kg_configs
    row = (await conn.execute(select(configs.c.knowledge).where(configs.c.project_id == project_id))).one_or_none()
    return None if row is None else row[0]


async def run_ids(conn: AsyncConnection, runs, project_id: int) -> list[str]:
    """The run ids of the project in ``runs`` (kg_ingests or kg_pending_runs), as text, in order."""
    run_id = cast(runs.c.run_id, Text).label("run_id")
    query = select(run_id).where(runs.c.project_id == project_id).order_by(run_id)
    return list((await conn.execute(query)).scalars().all())


async def missing(conn: AsyncConnection, project_id: int, hashes) -> list[str]:
    """The hashes of ``hashes`` the project holds no blob of, sorted, each once."""
    blobs = tables.blobs
    wanted = func.unnest(literal(sorted(set(hashes)), ARRAY(Text))).table_valued("sha256").render_derived("r")
    held = exists().where(blobs.c.project_id == project_id, blobs.c.sha256 == wanted.c.sha256)
    query = select(wanted.c.sha256).where(~held).order_by(wanted.c.sha256)
    return list((await conn.execute(query)).scalars())


async def blob_size(conn: AsyncConnection, project_id: int, sha256: str) -> int | None:
    """The size of the project's blob ``sha256``; None when it holds none."""
    blobs = tables.blobs
    query = select(blobs.c.size).where(blobs.c.project_id == project_id, blobs.c.sha256 == sha256)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else row[0]


async def delete_upload(conn: AsyncConnection, project_id: int, upload_id: uuid.UUID, user_id: int) -> str | None:
    """Delete the row of an upload ``user_id`` asked for; its declared SHA-256, None when there was none."""
    uploads = tables.blob_uploads
    row = (
        await conn.execute(
            delete(uploads)
            .where(
                uploads.c.upload_id == upload_id,
                uploads.c.project_id == project_id,
                uploads.c.created_by == user_id,
            )
            .returning(uploads.c.sha256)
        )
    ).one_or_none()
    return row[0] if row else None


def facts(columns) -> tuple:
    """The columns of kg_pending_runs or kg_ingests that hold a run's facts (``kg_ingest.RunFacts.columns``)."""
    return (
        columns.status,
        columns.lineage,
        columns.epoch,
        columns.run_started_at,
        columns.run_finished_at,
        columns.cursor,
        columns.held,
        columns.listed,
    )


def _held_log(runs, project_id: int, run_id: uuid.UUID, *columns):
    """``columns`` of the run in ``runs`` (kg_ingests or kg_pending_runs)."""
    return select(*columns).where(runs.c.project_id == project_id, runs.c.run_id == run_id)


async def held_logs(conn: AsyncConnection, project_id: int, run_id: uuid.UUID):
    """(the row of the run's log in kg_ingests, its row in kg_pending_runs with its blobs), each None when not held."""
    ingests, pending_runs = tables.kg_ingests, tables.kg_pending_runs
    ingested = (await conn.execute(_held_log(ingests, project_id, run_id, ingests.c.log_sha256))).one_or_none()
    pending = (
        await conn.execute(_held_log(pending_runs, project_id, run_id, pending_runs.c.log_sha256, pending_runs.c.blobs))
    ).one_or_none()
    return ingested, pending


async def add_pending(conn: AsyncConnection, **values) -> None:
    """A row of kg_pending_runs, unless the run has one."""
    pending_runs = tables.kg_pending_runs
    await conn.execute(
        pg_insert(pending_runs)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[pending_runs.c.project_id, pending_runs.c.run_id])
    )


async def locked_pending(conn: AsyncConnection, project_id: int, run_id: uuid.UUID):
    """(source, log_sha256, log_size, blobs, *the facts) of the pending run, locked for update; None for none."""
    pending_runs = tables.kg_pending_runs
    p = pending_runs.c
    query = _held_log(pending_runs, project_id, run_id, p.source, p.log_sha256, p.log_size, p.blobs, *facts(p))
    return (await conn.execute(query.with_for_update())).one_or_none()


async def ingested(conn: AsyncConnection, project_id: int, run_id: uuid.UUID) -> bool:
    """Whether the run is in the project's corpus."""
    ingests = tables.kg_ingests
    query = _held_log(ingests, project_id, run_id, literal(1)).select_from(ingests)
    return (await conn.execute(query)).one_or_none() is not None


async def ingest(conn: AsyncConnection, project_id: int, run_id: uuid.UUID, **values) -> None:
    """Move the pending run into kg_ingests, with ``values``."""
    ingests, pending_runs = tables.kg_ingests, tables.kg_pending_runs
    await conn.execute(
        pg_insert(ingests)
        .values(project_id=project_id, run_id=run_id, **values)
        .on_conflict_do_nothing(index_elements=[ingests.c.project_id, ingests.c.run_id])
    )
    await conn.execute(
        delete(pending_runs).where(pending_runs.c.project_id == project_id, pending_runs.c.run_id == run_id)
    )


# Builds


def graph_kept(builds):
    """Whether a row of ``builds`` (kg_builds) is a succeeded build whose graph the hub's tables hold, or held until a
    newer build replaced it: the build of the project's main head (``kg_graph_rows.write_graph``) or an older one. A
    reader's snapshot may still be of an older one while the newest is loaded."""
    heads = tables.kg_graph_heads
    head = (
        select(heads.c.build_id)
        .where(heads.c.project_id == builds.c.project_id, heads.c.branch_id == MAIN)
        .scalar_subquery()
    )
    return func.coalesce(and_(builds.c.status == "succeeded", builds.c.id <= head), False)


async def find_builds(
    conn: AsyncConnection,
    project_id: int,
    *,
    build_id: int | None = None,
    status: str | None = None,
    limit: int = BUILDS_LIMIT,
) -> list[Build]:
    """The project's builds, newest first: only ``build_id`` and only those with ``status`` when given."""
    b, users = tables.kg_builds, tables.users
    query = (
        select(
            b.c.id,
            b.c.status,
            b.c.job_id,
            users.c.login.label("requested_by"),
            b.c.config_digest,
            b.c.runs,
            graph_kept(b).label("graph_kept"),
            b.c.artifact_sha256,
            b.c.artifact_size,
            b.c.artifact_reused_from,
            b.c.artifact_pruned_at,
            b.c.content_hash,
            b.c.nodes,
            b.c.edges,
            b.c.error,
            b.c.queued_at,
            b.c.started_at,
            b.c.finished_at,
        )
        .join_from(b, users, users.c.id == b.c.requested_by, isouter=True)
        .where(b.c.project_id == project_id)
    )
    if build_id is not None:
        query = query.where(b.c.id == build_id)
    if status is not None:
        query = query.where(b.c.status == status)
    rows = await conn.execute(query.order_by(b.c.id.desc()).limit(limit))
    builds = [Build(**row._mapping) for row in rows]
    if builds and await reads_chats(conn, project_id):  # totals that count chats would tell who is in none of them
        return [build.model_copy(update={"nodes": None, "edges": None}) for build in builds]
    return builds


async def queued_jobs(conn: AsyncConnection, project: str) -> list[Job]:
    """The build jobs of the project still in the queue, waiting or running, with the build each one runs."""
    j, b = procrastinate_jobs, tables.kg_builds
    query = (
        select(j.c.id.label("job_id"), cast(j.c.status, Text).label("status"), b.c.id.label("build_id"))
        .join_from(j, b, b.c.job_id == j.c.id, isouter=True)
        .where(j.c.lock == kg_lock(project), j.c.status.in_(["todo", "doing"]))
        .order_by(j.c.id)
    )
    return [Job(**row._mapping) for row in await conn.execute(query)]


async def built_graphs(conn: AsyncConnection, project_id: int, limit: int) -> list[BuiltGraph]:
    """The successful builds of a project whose graph the hub's tables hold or held (``graph_kept``), newest first, at
    most ``limit``: what the tools here and the web's graph pages (``evo_agents.hub.server.kg_web``) read from. It
    lives here rather than in ``evo_agents.hub.kg_graph``, which imports no database library."""
    builds = tables.kg_builds
    query = (
        select(
            builds.c.id.label("build_id"),
            builds.c.artifact_sha256.label("sha256"),
            builds.c.artifact_size.label("size"),
            builds.c.content_hash,
            builds.c.nodes,
            builds.c.edges,
            builds.c.finished_at,
            builds.c.coverage["vectors"].label("vectors"),
        )
        .where(builds.c.project_id == project_id, graph_kept(builds))
        .order_by(builds.c.id.desc())
        .limit(limit)
    )
    return [BuiltGraph(**row._mapping) for row in await conn.execute(query)]
