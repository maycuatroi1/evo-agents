"""The runs that map a project's entities to its code (``evo_agents.hub.server.kg_entity_map_run`` says what they
do): the phases ``advance`` moves a run through, starting and listing the runs, and the read of the latest graph the
agent's outline and changed items come from. Each phase and each request opens its own transactions with UnitOfWork;
the queries are ``kg.repository.entity_map_run``'s and ``evo_agents.hub.kg_entity_map_db``'s."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, kg_entity_map_db, runs
from evo_agents.hub import kg_entity_map_run as model
from evo_agents.hub.kg_graph import GraphUnavailable, read_labelled
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.server import audit, kg_init, kg_run_access
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg import (
    GRAPHS_TRIED,
    built_graphs,
    member,
    read_sink,
    reader_audience,
    snapshot_view,
    writer,
)
from evo_agents.hub.server.kg.models.entity_map_run import (
    ERROR_CHARS,
    KG_ENTITY_MAP,
    MAX_LISTED,
    EntityMapRun,
    EntityMapRuns,
    shown,
)
from evo_agents.hub.server.kg.repository import entity_map_run as repo
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_kg import ENTITY_MAP_ACTIVE

log = logging.getLogger(__name__)

Count = Callable[..., Awaitable[dict]]  # the read of the graph: (engine, row, blobs, data_dir, before) -> dict


# The job


async def _queue_agent(conn: AsyncConnection, ctx, who: tuple[int, str], body: dict, since_map: bool) -> int | None:
    """Queue the review run of ``ctx`` (lens kg_entity_map) as ``who`` and write its prompt, which tells the run's caps,
    into ``body["prompt"]`` (``since_map``: a run posted a map before); its id, or None with why in ``body["notes"]``."""
    from evo_agents.hub.server.run_state import notify_queued, write_event

    title = f"KG entity map of {ctx.project}"
    queued = await kg_init.queue_repoless(
        conn, ctx.project_id, ctx.project, who[0], title, body.setdefault("notes", [])
    )
    if queued is None:
        return None
    run_id, caps = queued
    body["prompt"] = model.prompt(
        project=ctx.project,
        run_id=ctx.id,
        sink=str(body.get("sink") or AGENT_SINK),
        found=model.Readable.of(body["readable"]),
        why=list(ctx.why or []),
        since_map=since_map,
        caps=caps,
    )
    text = (
        f"Queued for entity map run #{ctx.id} of project {ctx.project}, asked for as {ctx.via} by {who[1]}: lens "
        f"{model.LENS}; cost cap {curator.money(caps['max_usd'])}, {caps['max_turns']} turns, "
        f"{caps['max_seconds'] // 60} minutes of agent time."
    )
    await write_event(conn, run_id, {"text": text, "entity_map_run_id": ctx.id, "lenses": [model.LENS], **caps})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=who[0],
        token_id=None,
        action=KG_ENTITY_MAP,
        target=f"{ctx.project} run:{run_id} entity_map={ctx.id}",
        project_id=ctx.project_id,
    )
    return run_id


class MapFailed(Exception):
    pass


async def _queued(engine: AsyncEngine, ctx, blobs, data_dir, now: datetime, count: Count) -> str:
    """A run not yet given to an agent: read what its sink reads, write the prompt, queue the review run."""
    async with UnitOfWork(engine) as conn:
        latest = await repo.latest_build(conn, ctx.project_id)
        who = await kg_init.asker_of(conn, ctx)
        before = await kg_entity_map_db.last_revs(conn, ctx.project_id)
    if latest is None:
        raise MapFailed("the project has no successful build to map")
    if who is None:
        raise MapFailed("the project has no admin to dispatch the review run as")
    body = dict(ctx.body or {})
    revs = ctx.revs
    if body.get("readable") is None:
        row = SimpleNamespace(project_id=ctx.project_id, project=ctx.project, owner_id=who[0], owner=who[1])
        found = await count(engine, row, blobs, data_dir, before)
        if found.get("why"):
            raise MapFailed(f"the graph could not be read through the agents' sink: {found['why']}")
        body |= {"sink": found.get("sink"), "readable": found["readable"]}
        revs = found["revs"]
    readable = model.Readable.of(body["readable"])
    async with UnitOfWork(engine) as conn:
        body["notes"] = [n for n in body.get("notes") or [] if not n.startswith("no review run")]
        agent_run_id = await _queue_agent(conn, ctx, who, body, before is not None)
        if agent_run_id is None:
            if ctx.created_at + model.QUEUE_WAIT < now:
                raise MapFailed("no review run could be queued in " + str(model.QUEUE_WAIT))
            await repo.set_run(conn, ctx.id, body=body, build_id=latest.id, revs=revs)
            return "waiting"
        await repo.set_run(
            conn, ctx.id, state="running", body=body, build_id=latest.id, revs=revs, agent_run_id=agent_run_id
        )
    log.info(
        "kg entity map review run queued",
        extra={"project": ctx.project, "entity_map_run_id": ctx.id, "run_id": agent_run_id, "items": readable.items},
    )
    return "queued"


async def _running(engine: AsyncEngine, ctx) -> str:
    async with UnitOfWork(engine) as conn:
        state = await repo.run_state(conn, ctx.agent_run_id) if ctx.agent_run_id else None
        if state in runs.ACTIVE_STATES:
            return "running"
        maps = await repo.maps_of(conn, ctx.id)
        error = None if maps else f"the review run ended ({state or 'gone'}) without posting a map"
        await _end(conn, ctx, dict(ctx.body or {}), "done" if maps else "failed", error)
    return "done" if maps else "failed"


async def _end(conn: AsyncConnection, ctx, body: dict, state: str, error: str | None) -> None:
    await repo.set_run(conn, ctx.id, state=state, body=body, error=error and error[:ERROR_CHARS], ended_at=func.now())


async def _fail(engine: AsyncEngine, ctx, error: str) -> None:
    async with UnitOfWork(engine) as conn:
        body = dict(ctx.body or {})
        body.setdefault("notes", []).append(f"failed: {error}")
        await _end(conn, ctx, body, "failed", error)


async def advance(engine: AsyncEngine, *, count: Count, blobs=None, data_dir=None, now: datetime | None = None) -> dict:
    """Move each entity map run in flight one phase on, each on its own; how many did what. ``count`` reads the graph
    (``kg_entity_map_run.count_readable``)."""
    async with UnitOfWork(engine) as conn:
        moment = now or await repo.now(conn)
        rows = await repo.active_contexts(conn)
    outcomes: dict[str, int] = {}
    for ctx in rows:
        try:
            if ctx.state == "queued":
                word = await _queued(engine, ctx, blobs, data_dir, moment, count)
            else:
                word = await _running(engine, ctx)
        except MapFailed as exc:
            await _fail(engine, ctx, str(exc))
            word = "failed"
        except Exception as exc:  # one run failing leaves the others their turn
            log.exception("a kg entity map run failed", extra={"project": ctx.project, "entity_map_run_id": ctx.id})
            await _fail(engine, ctx, f"{type(exc).__name__}: {exc}")
            word = "failed"
        outcomes[word] = outcomes.get(word, 0) + 1
    return dict(sorted(outcomes.items()))


# Starting and listing


async def start(conn: AsyncConnection, user: Principal, project: str) -> tuple[EntityMapRun, bool]:
    """Start an entity map run of ``project`` for ``user`` (a writer): (the run, True), or (the run in flight, False).
    HTTPException 409 when the latest build is not of strategy entity-graph-agent."""
    access = await project_access(conn, user, project)
    writer(access, "asking for an entity map run of")
    found = await kg_entity_map_db.active_of(conn, access.project_id)
    if found is not None:
        return shown(found, access.name), False
    latest = await repo.latest_build(conn, access.project_id)
    mapped = ((latest.coverage or {}).get("entity_map") if latest else None) or {}
    if latest is None or not mapped.get("enabled"):
        raise HTTPException(
            409,
            f"the latest build of project {access.name} is not of strategy entity-graph-agent: an entity map is read "
            "by that strategy alone; nothing was started",
        )
    row, made = await kg_entity_map_db.create(
        conn,
        access.project_id,
        "manual",
        [f"{user.login} asked for it"],
        requested_by=user.user_id,
        build_id=latest.id,
    )
    if made:
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=KG_ENTITY_MAP,
            target=f"{access.name} entity_map:{row.id}",
            project_id=access.project_id,
        )
        log.info("kg entity map run asked for", extra={"project": access.name, "login": user.login, "id": row.id})
    return shown(row, access.name), made


async def listing(conn: AsyncConnection, user: Principal, project: str, limit: int = MAX_LISTED) -> EntityMapRuns:
    access = await project_access(conn, user, project)
    member(access)
    found = [shown(row, access.name) for row in await repo.runs_of(conn, access.project_id, limit)]
    return EntityMapRuns(active=next((run for run in found if run.state in ENTITY_MAP_ACTIVE), None), runs=found)


async def start_run(engine: AsyncEngine, user: Principal, project: str) -> tuple[EntityMapRun, bool]:
    """``start`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await start(conn, user, project)


async def list_runs(engine: AsyncEngine, user: Principal, project: str, limit: int = MAX_LISTED) -> EntityMapRuns:
    """``listing`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await listing(conn, user, project, limit)


# What the agent reads before it maps


async def read_graph(state, user: Principal, project: str, read) -> dict:
    """``read(store, clearance, before)`` on the project's latest graph as ``user`` reads it through the agents' sink
    (a KG run's token reads through it whatever it asks), ``before`` the snapshot of the last run that posted a map;
    ``state`` is the app's."""
    async with UnitOfWork(state.engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
        before = await kg_entity_map_db.last_revs(conn, access.project_id)
    if not graphs:
        raise HTTPException(409, f"project {project} has no knowledge graph on the hub yet: build it first")
    sink = kg_run_access.sink(user, AGENT_SINK)
    ceiling = access.rules.ceiling(access.max_level, read_sink(access, sink), audience)
    if ceiling is None:
        raise HTTPException(409, f"nothing of project {project} is visible through sink {sink!r} with your grant")
    snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    try:
        found = await asyncio.to_thread(
            read_labelled,
            project,
            ceiling,
            graphs,
            lambda s, c: read(s, c, before),
            snapshot,
        )
    except GraphUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    return {"project": project, "sink": sink, **found}
