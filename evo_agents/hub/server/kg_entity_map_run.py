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

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_entity_map_run as model
from evo_agents.hub.server import kg_questions
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.entity_map_run import (  # noqa: F401 (read from here)
    KG_ENTITY_MAP,
    MAX_LISTED,
    EntityMapRun,
    EntityMapRuns,
)
from evo_agents.hub.server.kg.service import entity_map_run as service
from evo_agents.hub.server.kg.service.entity_map_run import listing, start  # noqa: F401 (read from here)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})


# The job


async def count_readable(engine: AsyncEngine, row, blobs, data_dir: Path | None, before: dict | None) -> dict:
    """What the agents' sink of ``row``'s owner reads of the project's latest graph: {"sink", "readable", "revs"}, or
    {"why": ...}."""

    def read(store, ceiling) -> dict:
        found, revs = model.readable(store, ceiling, before)
        return {"readable": found.to_json(), "revs": revs}

    return await kg_questions._read_graph(engine, row, blobs, data_dir, read)


async def advance(engine: AsyncEngine, *, blobs=None, data_dir=None, now: datetime | None = None, count=None) -> dict:
    """Move each entity map run in flight one phase on (see the module), each on its own; how many did what.
    ``count`` stands for the read of the graph (``count_readable``)."""
    return await service.advance(engine, count=count or count_readable, blobs=blobs, data_dir=data_dir, now=now)


# The routes


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


REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@router.post(
    "/{project}/entity-map/run",
    response_model=EntityMapRun,
    status_code=201,
    responses={200: {"model": EntityMapRun, "description": "a run is in flight already: that run"}, **REFUSALS},
)
async def start_entity_map(request: Request, project: ProjectName, user: CurrentUser):
    """Ask for an entity map run of the project now: 201 with the new run, or 200 with the run in flight."""
    found, started = await service.start_run(request.app.state.engine, user, project)
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
    return await service.list_runs(request.app.state.engine, user, project, limit)


@router.get(
    "/{project}/entity-map/outline", response_model=EntityMapOutline, responses=REFUSALS | {502: {"model": ErrorBody}}
)
async def entity_map_outline(request: Request, project: ProjectName, user: CurrentUser) -> EntityMapOutline:
    """The structure of the project as the agents' sink reads its latest graph: repos and directories with their files
    and symbols, the clusters that hold code, the entities, the documents and the plans."""
    return EntityMapOutline(
        **await service.read_graph(
            request.app.state, user, project, lambda store, clearance, _: model.outline(store, clearance)
        )
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
    found = await service.read_graph(
        request.app.state,
        user,
        project,
        lambda store, clearance, before: model.changed(store, clearance, before, limit),
    )
    return EntityMapChanged(**found)
