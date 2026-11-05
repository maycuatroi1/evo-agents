"""The runs that map a project's entities to its code (plan kg-entities-to-code, criterion a3);
``evo_agents.hub.kg_entity_map_run`` holds the rules without the database, ``evo_agents.hub.kg_entity_map_db`` the rows.

- ``advance``, each minute in ``curator.collect``: a run still ``queued`` reads the latest graph as the agents' sink of
  the user it is dispatched as reads it (who asked, else the project's first admin), keeps the revision of each item
  it read (``revs``), writes its prompt (counts and classes only) and queues a review run with lens kg_entity_map on
  the project's default agent pool under the charter's review cost cap (``kg_init.queue_repoless``). One that no node
  could take in ``QUEUE_WAIT`` fails. A run ``running`` ends when its review run does: done when the run posted a map
  (POST /v1/kg/{project}/entity-map counts them, ``kg_entity_map_db.posted``), failed otherwise.
- POST /v1/kg/{project}/entity-map/run (``evo-agents hub kg entity-map --run``): a writer asks for a run now; 201 with
  the new run, 200 with the run in flight. GET /v1/kg/{project}/entity-map/runs lists the runs.
- GET /v1/kg/{project}/entity-map/outline and .../changed: what the agent reads before it maps
  (``kg_entity_map_run.outline`` and ``changed``), read with the caller's grant through the agents' sink, as the map it
  posts is checked.

Logs name the project and the runs, and count; never an item, a path or an entity.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, kg_entity_map_db, runs, tables
from evo_agents.hub import kg_entity_map_run as model
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphOutdated, GraphUnavailable, read_labelled
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.server import audit, kg_init, kg_questions, kg_run_access
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import (
    GRAPHS_TRIED,
    built_graphs,
    member,
    read_sink,
    reader_audience,
    rebuild_outdated,
    writer,
)
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_kg import ENTITY_MAP_ACTIVE

log = logging.getLogger(__name__)

KG_ENTITY_MAP = "kg.entity_map"  # the audit action of an asked-for run and of its review run
MAX_LISTED = 10
ERROR_CHARS = 2000

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})


# The job


async def _set(conn: AsyncConnection, run_id: int, **values) -> None:
    t = tables.kg_entity_map_runs
    await conn.execute(update(t).where(t.c.id == run_id).values(**values, updated_at=func.now()))


def _context():
    """The runs with their project's name."""
    t, p = tables.kg_entity_map_runs, tables.projects
    return select(t, p.c.name.label("project")).join_from(t, p, p.c.id == t.c.project_id)


async def _latest_build(conn: AsyncConnection, project_id: int):
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.coverage)
        .where(b.c.project_id == project_id, b.c.status == "succeeded")
        .order_by(b.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def count_readable(engine: AsyncEngine, row, blobs, data_dir: Path | None, before: dict | None) -> dict:
    """What the agents' sink of ``row``'s owner reads of the project's latest graph: {"sink", "readable", "revs"}, or
    {"why": ...}."""

    def read(store, ceiling) -> dict:
        found, revs = model.readable(store, ceiling, before)
        return {"readable": found.to_json(), "revs": revs}

    return await kg_questions._read_graph(engine, row, blobs, data_dir, read)


async def _queue_agent(conn: AsyncConnection, ctx, who: tuple[int, str], body: dict) -> int | None:
    """Queue the review run of ``ctx`` (lens kg_entity_map) as ``who``; its id, or None with why in ``body["notes"]``."""
    from evo_agents.hub.server.run_state import notify_queued, write_event

    title = f"KG entity map of {ctx.project}"
    queued = await kg_init.queue_repoless(
        conn, ctx.project_id, ctx.project, who[0], title, body.setdefault("notes", [])
    )
    if queued is None:
        return None
    run_id, caps = queued
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


async def _queued(engine: AsyncEngine, ctx, blobs, data_dir, now: datetime, count) -> str:
    """A run not yet given to an agent: read what its sink reads, write the prompt, queue the review run."""
    async with engine.begin() as conn:
        latest = await _latest_build(conn, ctx.project_id)
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
    body["prompt"] = model.prompt(
        project=ctx.project,
        run_id=ctx.id,
        sink=str(body.get("sink") or AGENT_SINK),
        found=readable,
        why=list(ctx.why or []),
        since_map=before is not None,
    )
    async with engine.begin() as conn:
        body["notes"] = [n for n in body.get("notes") or [] if not n.startswith("no review run")]
        agent_run_id = await _queue_agent(conn, ctx, who, body)
        if agent_run_id is None:
            if ctx.created_at + model.QUEUE_WAIT < now:
                raise MapFailed("no review run could be queued in " + str(model.QUEUE_WAIT))
            await _set(conn, ctx.id, body=body, build_id=latest.id, revs=revs)
            return "waiting"
        await _set(conn, ctx.id, state="running", body=body, build_id=latest.id, revs=revs, agent_run_id=agent_run_id)
    log.info(
        "kg entity map review run queued",
        extra={"project": ctx.project, "entity_map_run_id": ctx.id, "run_id": agent_run_id, "items": readable.items},
    )
    return "queued"


async def _running(engine: AsyncEngine, ctx) -> str:
    r, t = tables.runs, tables.kg_entity_map_runs
    async with engine.begin() as conn:
        state = await conn.scalar(select(r.c.state).where(r.c.id == ctx.agent_run_id)) if ctx.agent_run_id else None
        if state in runs.ACTIVE_STATES:
            return "running"
        maps = int(await conn.scalar(select(t.c.maps).where(t.c.id == ctx.id)) or 0)
        error = None if maps else f"the review run ended ({state or 'gone'}) without posting a map"
        await _end(conn, ctx, dict(ctx.body or {}), "done" if maps else "failed", error)
    return "done" if maps else "failed"


async def _end(conn: AsyncConnection, ctx, body: dict, state: str, error: str | None) -> None:
    await _set(conn, ctx.id, state=state, body=body, error=error and error[:ERROR_CHARS], ended_at=func.now())


async def _fail(engine: AsyncEngine, ctx, error: str) -> None:
    async with engine.begin() as conn:
        body = dict(ctx.body or {})
        body.setdefault("notes", []).append(f"failed: {error}")
        await _end(conn, ctx, body, "failed", error)


async def advance(engine: AsyncEngine, *, blobs=None, data_dir=None, now: datetime | None = None, count=None) -> dict:
    """Move each entity map run in flight one phase on (see the module), each on its own; how many did what.
    ``count`` stands for the read of the graph (``count_readable``)."""
    count = count or count_readable
    t = tables.kg_entity_map_runs
    async with engine.begin() as conn:
        moment = now or await conn.scalar(select(func.now()))
        rows = (await conn.execute(_context().where(t.c.state.in_(ENTITY_MAP_ACTIVE)).order_by(t.c.id))).all()
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


# The routes


class EntityMapRun(BaseModel):
    id: int
    project: str
    via: Literal["first_build", "stale", "manual"]
    state: Literal["queued", "running", "done", "failed"]
    why: list[str] = Field(description="what asked for the run")
    build_id: int | None
    agent_run_id: int | None = Field(description="the review run (lens kg_entity_map) of the agent that maps")
    maps: int = Field(description="the maps the review run posted")
    readable: dict | None = Field(description="what the run's sink read of the graph when it was queued, in counts")
    notes: list[str]
    error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


class EntityMapRuns(BaseModel):
    active: EntityMapRun | None
    runs: list[EntityMapRun]


def _shown(row, project: str) -> EntityMapRun:
    body = model.shown(row.body if isinstance(row.body, dict) else {})
    return EntityMapRun(
        id=row.id,
        project=project,
        via=row.via,
        state=row.state,
        why=list(row.why or []),
        build_id=row.build_id,
        agent_run_id=row.agent_run_id,
        maps=row.maps,
        readable=body.get("readable"),
        notes=[str(n) for n in body.get("notes") or []],
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
    )


async def start(conn: AsyncConnection, user: Principal, project: str) -> tuple[EntityMapRun, bool]:
    """Start an entity map run of ``project`` for ``user`` (a writer): (the run, True), or (the run in flight, False).
    HTTPException 409 when the latest build is not of strategy entity-graph-agent."""
    access = await project_access(conn, user, project)
    writer(access, "asking for an entity map run of")
    found = await kg_entity_map_db.active_of(conn, access.project_id)
    if found is not None:
        return _shown(found, access.name), False
    latest = await _latest_build(conn, access.project_id)
    mapped = ((latest.coverage or {}).get("entity_map") if latest else None) or {}
    if not mapped.get("enabled"):
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
    return _shown(row, access.name), made


async def listing(conn: AsyncConnection, user: Principal, project: str, limit: int = MAX_LISTED) -> EntityMapRuns:
    access = await project_access(conn, user, project)
    member(access)
    t = tables.kg_entity_map_runs
    rows = (
        await conn.execute(select(t).where(t.c.project_id == access.project_id).order_by(t.c.id.desc()).limit(limit))
    ).all()
    shown = [_shown(row, access.name) for row in rows]
    return EntityMapRuns(active=next((run for run in shown if run.state in ENTITY_MAP_ACTIVE), None), runs=shown)


async def _read(request: Request, user: Principal, project: str, read):
    """``read(store, clearance, before)`` on the project's latest graph as ``user`` reads it through the agents' sink
    (a KG run's token reads through it whatever it asks), ``before`` the snapshot of the last run that posted a map."""
    state = request.app.state
    async with state.engine.begin() as conn:
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
    try:
        found = await asyncio.to_thread(
            read_labelled, state.kg_graphs, state.blobs, project, ceiling, graphs, lambda s, c: read(s, c, before)
        )
    except GraphOutdated as exc:
        await rebuild_outdated(state, access.project_id, project)
        raise HTTPException(409, str(exc)) from None
    except GraphUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    except ArtifactMismatch as exc:
        raise HTTPException(502, str(exc)) from None
    return {"project": project, "sink": sink, **found}


class EntityMapOutline(BaseModel):
    project: str
    sink: str = Field(description="the sink the graph was read through")
    counts: dict[str, int] = Field(description="each list counted whole, before it was cut")
    repos: list[dict] = Field(description="each repo with its files and symbols")
    directories: list[dict] = Field(description="each directory (repo, path) with the files and symbols under it")
    clusters: list[dict] = Field(description="the clusters that hold code: id, level, code and other nodes, members")
    entities: list[dict] = Field(description="the entities the graph has, with the code each is implemented by")
    documents: list[dict] = Field(description="the documents with their sections")
    plans: list[dict] = Field(description="the plans with their steps")


class EntityMapChanged(BaseModel):
    project: str
    sink: str = Field(description="the sink the graph was read through")
    since_map: bool = Field(description="whether a run posted a map before; false: every item is new")
    counts: dict[str, int] = Field(description="each list counted whole, before it was cut")
    new: list[dict] = Field(description="the items no map's snapshot holds: id and class")
    changed: list[dict] = Field(description="the items whose revision changed since the snapshot")
    gone: list[dict] = Field(description="the items of the snapshot the graph no longer holds")
    uncited: list[dict] = Field(description="the documents, sections, plans and steps no map cites")


REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@router.post(
    "/{project}/entity-map/run",
    response_model=EntityMapRun,
    status_code=201,
    responses={200: {"model": EntityMapRun, "description": "a run is in flight already: that run"}, **REFUSALS},
)
async def start_entity_map(request: Request, project: ProjectName, user: CurrentUser):
    """Ask for an entity map run of the project now: 201 with the new run, or 200 with the run in flight."""
    async with request.app.state.engine.begin() as conn:
        found, started = await start(conn, user, project)
    if started:
        return found
    return JSONResponse(found.model_dump(mode="json"), status_code=200)


@router.get("/{project}/entity-map/runs", response_model=EntityMapRuns, responses=REFUSALS)
async def list_entity_map(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50, description="the newest runs listed")] = MAX_LISTED,
) -> EntityMapRuns:
    """The project's entity map runs, newest first, with the one in flight."""
    async with request.app.state.engine.begin() as conn:
        return await listing(conn, user, project, limit)


@router.get(
    "/{project}/entity-map/outline", response_model=EntityMapOutline, responses=REFUSALS | {502: {"model": ErrorBody}}
)
async def entity_map_outline(request: Request, project: ProjectName, user: CurrentUser) -> EntityMapOutline:
    """The structure of the project as the agents' sink reads its latest graph: repos and directories with their files
    and symbols, the clusters that hold code, the entities, the documents and the plans."""
    return EntityMapOutline(
        **await _read(request, user, project, lambda store, clearance, _: model.outline(store, clearance))
    )


@router.get(
    "/{project}/entity-map/changed", response_model=EntityMapChanged, responses=REFUSALS | {502: {"model": ErrorBody}}
)
async def entity_map_changed(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=model.MAX_CHANGED, description="the items of each list")] = 500,
) -> EntityMapChanged:
    """The items new, changed or gone since the last run that posted a map, and the documents, sections, plans and
    steps no map cites, as the agents' sink reads the latest graph."""
    found = await _read(
        request, user, project, lambda store, clearance, before: model.changed(store, clearance, before, limit)
    )
    return EntityMapChanged(**found)
