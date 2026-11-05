"""The knowledge config of a project's graph and the sources it reads (``evo_agents.hub.server.kg``): PUT config saves
the config a build reads, refusing one that drops or raises above the hub sink a source the hub holds runs of, and
DELETE sources/{source} takes every run of a source off the hub first.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, null, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, kg_upload_sources, tables
from evo_agents.hub.kg_ingest import config_digest, config_problems
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import LOG_KIND, REFUSALS, Queued, SourceId, find_builds, hub_ceiling, queue, writer
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.policy import Policy

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})


class KgConfig(BaseModel):
    knowledge: dict = Field(
        description="policy, identifiers, ontology file name and sources {id, connector, label, code, repo, ref}"
    )
    ontology: dict | None = Field(None, description="the ontology extension knowledge.yaml points at, if any")


class ConfigSaved(BaseModel):
    digest: str
    changed: bool = Field(description="false when the hub held exactly this config")


class SourceForgotten(Queued):
    source: str
    ingested: int = Field(description="runs of the source taken out of the project's corpus")
    pending: int = Field(description="runs of the source that waited for their blobs, dropped")
    logs: int = Field(description="their logs nothing else refers to, deleted from the blob store by the next prune")
    declared: bool = Field(
        description="whether the knowledge config on the hub still declares the source: a machine holding its runs "
        "then pushes them again"
    )
    uploads: int = Field(0, description="uploaded archives of the source deleted, with their items")


# The config


def _held_sources(project_id: int):
    """The sources of the runs the hub holds for the project, ingested or pending, each once, by name."""
    ingests, pending = tables.kg_ingests, tables.kg_pending_runs
    sources = union(
        select(ingests.c.source).where(ingests.c.project_id == project_id),
        select(pending.c.source).where(pending.c.project_id == project_id),
    )
    return sources.order_by(sources.selected_columns.source)


def forget_command(project: str, sources: list[str]) -> str:
    """The command that takes every run of ``sources`` off the hub (``forget_source``)."""
    return f"`evo-agents hub kg forget-source --project {project} " + " ".join(f"--source {s}" for s in sources) + "`"


def _declared(knowledge: dict) -> set:
    return {source.get("id") for source in knowledge.get("sources") or [] if isinstance(source, dict)}


def _uncleared_message(project: str, knowledge: dict, uncleared: list[str]) -> str:
    """Why a config is refused whose ``uncleared`` sources the hub holds runs of: a source the config no longer
    declares takes the highest label, so dropping one needs its runs taken off the hub first."""
    declared = _declared(knowledge)
    dropped = [source for source in uncleared if source not in declared]
    raised = [source for source in uncleared if source in declared]
    parts = []
    if dropped:
        parts.append(
            f"the config drops sources {', '.join(dropped)}, but the hub holds runs of them: take them off the hub "
            f"first with {forget_command(project, dropped)}, then push again"
        )
    if raised:
        parts.append(
            f"sources {', '.join(raised)} would be labelled above what the hub sink of project {project} clears, but "
            "the hub holds runs of them: lower their labels, raise the sink's clearance, or take their runs off the "
            f"hub with {forget_command(project, raised)}"
        )
    return "; ".join(parts)


@router.put("/{project}/config", response_model=ConfigSaved, responses=REFUSALS)
async def put_config(request: Request, project: ProjectName, body: KgConfig, user: CurrentUser) -> ConfigSaved:
    """Save the knowledge config the project's graph is built with."""
    config = body.model_dump()
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        ceiling = hub_ceiling(access)
        access.push_label(None)  # the config is the harness source's: its label must leave the machine too
        problems = config_problems(project, access.rules.levels, access.rules.locations, config)
        if problems:
            raise HTTPException(
                422, f"the knowledge config of project {project} cannot be used: " + "; ".join(problems)
            )
        policy = Policy(project, config["knowledge"])
        held_sources = await conn.execute(_held_sources(access.project_id))
        uncleared = [source for source in held_sources.scalars() if not policy.source_label(source).below(ceiling)]
        if uncleared:
            raise HTTPException(422, _uncleared_message(project, config["knowledge"], uncleared))
        digest = config_digest(config)
        configs = tables.kg_configs
        held = (
            await conn.execute(
                select(configs.c.digest).where(configs.c.project_id == access.project_id).with_for_update()
            )
        ).one_or_none()
        if held is not None and held[0] == digest:
            return ConfigSaved(digest=digest, changed=False)
        saved = pg_insert(configs).values(
            project_id=access.project_id,
            digest=digest,
            knowledge=config["knowledge"],
            ontology=null() if config["ontology"] is None else config["ontology"],  # SQL NULL, not JSON null
            updated_by=user.user_id,
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
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_CONFIG, target=project)
    log.info("kg config saved", extra={"project": project, "login": user.login, "digest": digest})
    return ConfigSaved(digest=digest, changed=True)


# Forgetting a source


async def _drop_source(conn: AsyncConnection, project_id: int, source: str) -> tuple[int, int, int]:
    """Delete the project's runs of ``source``, pending ones first (a commit in progress finishes before), then
    ingested ones, and the project's rows of their logs; record the logs nothing else refers to for deletion. In
    ``conn``'s transaction, which holds the blob lock exclusively. (ingested, pending, logs recorded)."""
    ingests, pending_runs, blobs = tables.kg_ingests, tables.kg_pending_runs, tables.blobs
    logs = {}
    counts = []
    for runs in (pending_runs, ingests):
        dropped = await conn.execute(
            delete(runs)
            .where(runs.c.project_id == project_id, runs.c.source == source)
            .returning(runs.c.log_sha256, runs.c.log_size)
        )
        rows = dropped.all()
        logs.update((row.log_sha256, row.log_size) for row in rows)
        counts.append(len(rows))
    if logs:  # a log names its run, so no other run of the project refers to it
        hashes = sorted(logs)
        await conn.execute(
            delete(blobs).where(
                blobs.c.project_id == project_id, blobs.c.kind == LOG_KIND, blob_gc.hashes_in(blobs.c.sha256, hashes)
            )
        )
        recorded = await blob_gc.forget(conn, {sha: (logs[sha], LOG_KIND) for sha in hashes})
    else:
        recorded = []
    return counts[1], counts[0], len(recorded)


@router.delete("/{project}/sources/{source}", response_model=SourceForgotten, responses=REFUSALS)
async def forget_source(request: Request, project: ProjectName, source: SourceId, user: CurrentUser) -> SourceForgotten:
    """Take every run of a source off the hub, ingested or pending, and queue a build without them: how a source the
    hub holds runs of leaves knowledge.yaml (PUT config refuses to drop it before)."""
    async with request.app.state.engine.connect() as conn, conn.begin():
        access = await project_access(conn, user, project)
        writer(access, "forgetting a source of")
        configs = tables.kg_configs
        knowledge = (
            await conn.execute(select(configs.c.knowledge).where(configs.c.project_id == access.project_id))
        ).scalar_one_or_none()
        declared = source in _declared(knowledge or {})
        await blob_gc.lock_exclusive(conn)
        ingested, pending, logs = await _drop_source(conn, access.project_id, source)
        uploads = await kg_upload_sources.forget(conn, access.project_id, source)
        build, queued = None, False
        if ingested or pending or uploads.uploads:
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.KG_FORGET,
                target=f"{project} source={source} runs={ingested + pending}"
                + (f" uploads={uploads.uploads}" if uploads.uploads else ""),
                project_id=access.project_id,
            )
            build_id, queued = await queue(request, conn, access.project_id, project, user.user_id)
            build = (await find_builds(conn, access.project_id, build_id=build_id))[0] if build_id is not None else None
    if uploads.keys or uploads.multiparts:
        await asyncio.to_thread(kg_upload_sources.release, blob_store(request), uploads)
    log.info(
        "kg source forgotten",
        extra={"project": project, "login": user.login, "runs": ingested + pending, "logs": logs, "queued": queued}
        | {"uploads": uploads.uploads},
    )
    return SourceForgotten(
        source=source,
        ingested=ingested,
        pending=pending,
        logs=logs,
        declared=declared,
        build=build,
        queued=queued,
        uploads=uploads.uploads,
    )
