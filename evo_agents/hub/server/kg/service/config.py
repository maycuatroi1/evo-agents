"""A project's knowledge config and the sources it reads (``evo_agents.hub.server.kg_config``): the checks and the
transaction of saving a config, which refuses one that drops or raises above the hub sink a source the hub holds runs
of, and the one transaction of taking every run of a source off the hub, which queues a build without them."""

from __future__ import annotations

from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blob_gc, kg_upload_sources
from evo_agents.hub.kg_ingest import config_digest, config_problems
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.config import ConfigSaved, KgConfig, SourceForgotten
from evo_agents.hub.server.kg.models.graph import LOG_KIND
from evo_agents.hub.server.kg.repository import config as repository
from evo_agents.hub.server.kg.repository.graph import find_builds, knowledge
from evo_agents.hub.server.kg.service.graph import hub_ceiling, queue, writer
from evo_agents.hub.server.projects import project_access
from evo_agents.kg.policy import Policy

# The config


def forget_command(project: str, sources: list[str]) -> str:
    """The command that takes every run of ``sources`` off the hub (``forget``)."""
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


async def save_config(engine: AsyncEngine, user, project: str, body: KgConfig) -> ConfigSaved:
    """Save ``body`` as the config the project's graph is built with; 422 for one a build cannot use or one that
    leaves runs the hub holds above the hub sink."""
    config = body.model_dump()
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        ceiling = hub_ceiling(access)
        access.push_label(None)  # the config is the harness source's: its label must leave the machine too
        problems = config_problems(project, access.rules.levels, access.rules.locations, config)
        if problems:
            raise HTTPException(
                422, f"the knowledge config of project {project} cannot be used: " + "; ".join(problems)
            )
        policy = Policy(project, config["knowledge"])
        held_sources = await repository.held_sources(conn, access.project_id)
        uncleared = [source for source in held_sources if not policy.source_label(source).below(ceiling)]
        if uncleared:
            raise HTTPException(422, _uncleared_message(project, config["knowledge"], uncleared))
        digest = config_digest(config)
        if await repository.locked_digest(conn, access.project_id) == digest:
            return ConfigSaved(digest=digest, changed=False)
        await repository.save(conn, access.project_id, config, digest, user.user_id)
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_CONFIG, target=project)
    return ConfigSaved(digest=digest, changed=True)


# Forgetting a source


async def _drop_source(conn: AsyncConnection, project_id: int, source: str) -> tuple[int, int, int]:
    """Delete the project's runs of ``source`` and the project's rows of their logs; record the logs nothing else
    refers to for deletion. In ``conn``'s transaction, which holds the blob lock exclusively. (ingested, pending, logs
    recorded)."""
    ingested, pending, logs = await repository.drop_runs(conn, project_id, source)
    if logs:  # a log names its run, so no other run of the project refers to it
        hashes = sorted(logs)
        await repository.drop_logs(conn, project_id, hashes)
        recorded = await blob_gc.forget(conn, {sha: (logs[sha], LOG_KIND) for sha in hashes})
    else:
        recorded = []
    return ingested, pending, len(recorded)


class Dropped(NamedTuple):
    """What ``forget`` answers, and the uploads whose objects the caller releases from the blob store after it."""

    out: SourceForgotten
    uploads: kg_upload_sources.Forgotten


async def forget(engine: AsyncEngine, jobs, user, project: str, source: str) -> Dropped:
    """Take every run of ``source`` off the hub, ingested or pending, with its uploads, and queue a build without them
    through the app's ``jobs``, for a writer."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        writer(access, "forgetting a source of")
        declared = source in _declared(await knowledge(conn, access.project_id) or {})
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
            build_id, queued = await queue(jobs, conn, access.project_id, project, user.user_id)
            build = (await find_builds(conn, access.project_id, build_id=build_id))[0] if build_id is not None else None
    out = SourceForgotten(
        source=source,
        ingested=ingested,
        pending=pending,
        logs=logs,
        declared=declared,
        build=build,
        queued=queued,
        uploads=uploads.uploads,
    )
    return Dropped(out, uploads)
