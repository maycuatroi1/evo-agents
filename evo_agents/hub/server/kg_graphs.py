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

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.graphs import (
    GraphName,
    NamedGraphCreate,
    NamedGraphDeleted,
    NamedGraphOut,
    NamedGraphsOut,
    NamedGraphUpdate,
)
from evo_agents.hub.server.kg.service import graphs as service
from evo_agents.hub.server.kg.service.graphs import may_change as may_change
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422)},
)


@router.get("/{project}/graphs", response_model=NamedGraphsOut)
async def list_graphs(request: Request, project: ProjectName, user: CurrentUser) -> NamedGraphsOut:
    """``default`` and the named graphs of the project, with the sources its newest build read."""
    return await service.list_graphs(request.app.state.engine, user, project)


@router.post("/{project}/graphs", response_model=NamedGraphOut, status_code=201)
async def create_graph(
    request: Request, project: ProjectName, body: NamedGraphCreate, user: CurrentUser
) -> NamedGraphOut:
    """Add a named graph of some of the project's sources."""
    return await service.create_graph(request.app.state.engine, user, project, body)


@router.patch("/{project}/graphs/{name}", response_model=NamedGraphOut)
async def update_graph(
    request: Request, project: ProjectName, name: GraphName, body: NamedGraphUpdate, user: CurrentUser
) -> NamedGraphOut:
    """Rename a named graph, set its description or sources, give it sources or strip it of some."""
    return await service.update_graph(request.app.state.engine, user, project, name, body)


@router.delete("/{project}/graphs/{name}", response_model=NamedGraphDeleted)
async def delete_graph(request: Request, project: ProjectName, name: GraphName, user: CurrentUser) -> NamedGraphDeleted:
    """Take a named graph away; the project's build and its sources stay."""
    await service.delete_graph(request.app.state.engine, user, project, name)
    return NamedGraphDeleted(project=project, deleted=name)
