"""The named graphs of a project (``docs/kg.md``, Named graphs; ``evo_agents.hub.kg_named_graphs``).

A named graph is a name and a set of the project's sources (repos, uploads, connectors); one source can be in several.
The kg tools take ``graph`` and then answer from the nodes and edges of its sources alone, on the project's one build
(``evo_agents.kg.graph_scope``). ``default`` is every source and has no row.

- GET /v1/kg/{project}/graphs: ``default`` and the named graphs, with the sources of the newest build.
- POST /v1/kg/{project}/graphs {name, sources, description}: a writer, or the agent of a run of the project, adds one.
- PATCH /v1/kg/{project}/graphs/{name} {rename, description, sources, add, remove}: renames it, sets its sources, or
  gives it sources and strips it of others.
- DELETE /v1/kg/{project}/graphs/{name}: takes it away; the build and its sources stay.

Every write is audited as ``kg.graph``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import select

from evo_agents.hub import kg_named_graphs as store
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_web import require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.graph_scope import DEFAULT_GRAPH

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422)},
)

GraphName = Annotated[str, PathParam(min_length=1, max_length=63)]
SourceId = Annotated[str, Field(min_length=1, max_length=store.MAX_SOURCE_CHARS, pattern=store.SOURCE_PATTERN)]
SourceList = Annotated[list[SourceId], Field(max_length=store.MAX_SOURCES)]


class NamedGraphOut(BaseModel):
    name: str
    description: str
    sources: list[str] = Field(description="the sources it holds; empty for default, which holds every source")
    default: bool = Field(description="the implicit graph of every source of the project")
    missing: list[str] = Field(
        default_factory=list, description="sources it names that the newest build did not read: nothing of them shows"
    )
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NamedGraphsOut(BaseModel):
    graphs: list[NamedGraphOut] = Field(description="default first, then the named graphs by name")
    sources: list[str] = Field(description="the sources the newest build of the project read")
    can_change: bool


class NamedGraphCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63, description="a-z, 0-9, '.', '_', '-'; not 'default'")
    sources: SourceList = Field(default_factory=list)
    description: str = Field("", max_length=store.MAX_DESCRIPTION_CHARS)


class NamedGraphUpdate(BaseModel):
    rename: str | None = Field(None, min_length=1, max_length=63, description="its new name")
    description: str | None = Field(None, max_length=store.MAX_DESCRIPTION_CHARS)
    sources: SourceList | None = Field(None, description="its sources, all of them, in place of what it holds")
    add: SourceList = Field(default_factory=list, description="sources it gets")
    remove: SourceList = Field(default_factory=list, description="sources it loses")


async def may_change(conn, user, access) -> None:
    require_reader(access)
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"changing the named graphs of project {access.name} needs the writer role on it")
    run = user.acts_for_run
    if run is not None:
        project = (await conn.execute(select(tables.runs.c.project_id).where(tables.runs.c.id == run))).scalar()
        if project != access.project_id:
            raise HTTPException(403, f"run {run} is not a run of project {access.name}")


async def _logins(conn, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = await conn.execute(select(tables.users.c.id, tables.users.c.login).where(tables.users.c.id.in_(ids)))
    return {r.id: r.login for r in rows}


def _out(graph: store.NamedGraph, built: list[str], logins: dict[int, str]) -> NamedGraphOut:
    held = set(built)
    return NamedGraphOut(
        name=graph.name,
        description=graph.description,
        sources=graph.sources,
        default=False,
        missing=[s for s in graph.sources if s not in held] if built else [],
        created_by=logins.get(graph.created_by) if graph.created_by is not None else None,
        updated_by=logins.get(graph.updated_by) if graph.updated_by is not None else None,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
    )


async def _one_out(conn, project_id: int, graph: store.NamedGraph) -> NamedGraphOut:
    built = await store.built_sources(conn, project_id)
    logins = await _logins(conn, {i for i in (graph.created_by, graph.updated_by) if i is not None})
    return _out(graph, built, logins)


def _refused(e: Exception, project: str, name: str) -> HTTPException:
    if isinstance(e, store.NoSuchGraph):
        return HTTPException(404, f"project {project} has no graph named {name}: `evo-agents hub kg graph list`")
    if isinstance(e, store.GraphExists):
        return HTTPException(409, str(e))
    return HTTPException(422, str(e))


async def _audit(conn, user, access, project: str, name: str, kind: str, sources: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_GRAPH,
        target=f"{project} graph={name} kind={kind} sources={sources}",
        project_id=access.project_id,
    )


@router.get("/{project}/graphs", response_model=NamedGraphsOut)
async def list_graphs(request: Request, project: ProjectName, user: CurrentUser) -> NamedGraphsOut:
    """``default`` and the named graphs of the project, with the sources its newest build read."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        found = await store.listed(conn, access.project_id)
        built = await store.built_sources(conn, access.project_id)
        logins = await _logins(conn, {i for g in found for i in (g.created_by, g.updated_by) if i is not None})
    default = NamedGraphOut(name=DEFAULT_GRAPH, description="every source of the project", sources=[], default=True)
    return NamedGraphsOut(
        graphs=[default, *(_out(g, built, logins) for g in found)],
        sources=built,
        can_change=has_role(access.role, "writer"),
    )


@router.post("/{project}/graphs", response_model=NamedGraphOut, status_code=201)
async def create_graph(
    request: Request, project: ProjectName, body: NamedGraphCreate, user: CurrentUser
) -> NamedGraphOut:
    """Add a named graph of some of the project's sources."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            made = await store.create(
                conn, access.project_id, body.name, list(body.sources), body.description, user.user_id
            )
        except (store.GraphError, store.NoSuchGraph) as e:
            raise _refused(e, project, body.name) from e
        await _audit(conn, user, access, project, made.name, "create", len(made.sources))
        return await _one_out(conn, access.project_id, made)


@router.patch("/{project}/graphs/{name}", response_model=NamedGraphOut)
async def update_graph(
    request: Request, project: ProjectName, name: GraphName, body: NamedGraphUpdate, user: CurrentUser
) -> NamedGraphOut:
    """Rename a named graph, set its description or sources, give it sources or strip it of some."""
    if name == DEFAULT_GRAPH:
        raise HTTPException(422, f"'{DEFAULT_GRAPH}' is every source of the project and cannot change")
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            made = await store.change(
                conn,
                access.project_id,
                name,
                rename=body.rename,
                description=body.description,
                sources=None if body.sources is None else list(body.sources),
                add=list(body.add),
                remove=list(body.remove),
                user_id=user.user_id,
            )
        except (store.GraphError, store.NoSuchGraph) as e:
            raise _refused(e, project, name) from e
        kind = "update" if made.name == name else f"update renamed={name}"
        await _audit(conn, user, access, project, made.name, kind, len(made.sources))
        return await _one_out(conn, access.project_id, made)


class NamedGraphDeleted(BaseModel):
    project: str
    deleted: str


@router.delete("/{project}/graphs/{name}", response_model=NamedGraphDeleted)
async def delete_graph(request: Request, project: ProjectName, name: GraphName, user: CurrentUser) -> NamedGraphDeleted:
    """Take a named graph away; the project's build and its sources stay."""
    if name == DEFAULT_GRAPH:
        raise HTTPException(422, f"'{DEFAULT_GRAPH}' is every source of the project and cannot be deleted")
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            await store.remove(conn, access.project_id, name)
        except store.NoSuchGraph as e:
            raise _refused(e, project, name) from e
        await _audit(conn, user, access, project, name, "delete", 0)
    return NamedGraphDeleted(project=project, deleted=name)
