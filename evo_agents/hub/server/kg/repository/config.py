"""The SQL of a project's knowledge config and of taking a source off the hub (``evo_agents.hub.server.kg_config``):
the sources the hub holds runs of, the config row, and the runs of a source with the rows of their logs."""

from __future__ import annotations

from sqlalchemy import delete, func, null, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.server.kg.models.graph import LOG_KIND


async def held_sources(conn: AsyncConnection, project_id: int) -> list[str]:
    """The sources of the runs the hub holds for the project, ingested or pending, each once, by name."""
    ingests, pending = tables.kg_ingests, tables.kg_pending_runs
    sources = union(
        select(ingests.c.source).where(ingests.c.project_id == project_id),
        select(pending.c.source).where(pending.c.project_id == project_id),
    )
    return list((await conn.execute(sources.order_by(sources.selected_columns.source))).scalars())


async def locked_digest(conn: AsyncConnection, project_id: int) -> str | None:
    """The digest of the config the hub holds for the project, its row locked; None without one."""
    configs = tables.kg_configs
    held = (
        await conn.execute(select(configs.c.digest).where(configs.c.project_id == project_id).with_for_update())
    ).one_or_none()
    return None if held is None else held[0]


async def save(conn: AsyncConnection, project_id: int, config: dict, digest: str, user_id: int | None) -> None:
    """Insert or replace the project's config."""
    configs = tables.kg_configs
    saved = pg_insert(configs).values(
        project_id=project_id,
        digest=digest,
        knowledge=config["knowledge"],
        ontology=null() if config["ontology"] is None else config["ontology"],  # SQL NULL, not JSON null
        updated_by=user_id,
    )
    await conn.execute(
        saved.on_conflict_do_update(
            index_elements=[configs.c.project_id],
            set_={
                "digest": saved.excluded.digest,
                "knowledge": saved.excluded.knowledge,
                "ontology": saved.excluded.ontology,
                "updated_by": saved.excluded.updated_by,
                "updated_at": func.now(),
            },
        )
    )


async def drop_runs(conn: AsyncConnection, project_id: int, source: str) -> tuple[int, int, dict[str, int]]:
    """Delete the project's runs of ``source``, pending ones first (a commit in progress finishes before), then
    ingested ones: (ingested, pending, {log sha256: size} of their logs)."""
    logs = {}
    counts = []
    for runs in (tables.kg_pending_runs, tables.kg_ingests):
        dropped = await conn.execute(
            delete(runs)
            .where(runs.c.project_id == project_id, runs.c.source == source)
            .returning(runs.c.log_sha256, runs.c.log_size)
        )
        rows = dropped.all()
        logs.update((row.log_sha256, row.log_size) for row in rows)
        counts.append(len(rows))
    return counts[1], counts[0], logs


async def drop_logs(conn: AsyncConnection, project_id: int, hashes: list[str]) -> None:
    """Delete the project's rows of the logs ``hashes``."""
    blobs = tables.blobs
    await conn.execute(
        delete(blobs).where(
            blobs.c.project_id == project_id, blobs.c.kind == LOG_KIND, blob_gc.hashes_in(blobs.c.sha256, hashes)
        )
    )
