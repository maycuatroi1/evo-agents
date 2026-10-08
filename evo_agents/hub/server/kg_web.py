"""Read-only routes of a project's knowledge graph for the hub's web pages.

- GET /v1/kg/{project}/graph: the build the reads answer from, and how many visible nodes of each kind it holds.
- GET /v1/kg/{project}/nodes?q=&kind=&limit=: search, as kg_search.
- GET /v1/kg/{project}/node?id=: one node with its label, evidence (source, revision, span, uri) and its visible
  neighbours by edge type, as kg_node.
- GET /v1/kg/{project}/neighbourhood?id=&hops=&limit=: the nodes and edges the graph view draws, at most 2 steps
  from the node and at most 150 nodes, saying when it left nodes out.

The build status the pages show comes from GET /v1/kg/{project}/builds (``evo_agents.hub.server.kg``). Nothing here
writes, queues a build or goes through /mcp.

Every route needs a grant on the project, as the kg_* tools do (404 for a project the caller cannot see, 403 for a
hub admin without a grant), and reads with the grant's max level alone (``ProjectRules.grant_label``, the label of
the hub's one grant-only read rule, ``visible_by_grant`` in ``evo_agents.hub.access``): the web shows the member's
own data in their browser, so no sink's clearance applies. Visibility itself is the tools' one filter
(``evo_agents.hub.kg_web``). A node that is missing and a node above the member's level both answer 404.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from evo_agents.hub.kg_graph import ArtifactMismatch, BuiltGraph, GraphUnavailable
from evo_agents.hub.kg_web import MAX_HOPS, MAX_NODES, SEARCH_LIMIT, GraphProblem, NotVisible, WebSession, read
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

GRAPHS_TRIED = 10  # successful builds holding an artifact tried, newest first, when the newest cannot be fetched

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 502, 503)},
)

NodeId = Annotated[str, Query(alias="id", min_length=1, max_length=1000, description="a node id or alias")]


class GraphRef(BaseModel):
    build_id: int
    content_hash: str
    nodes: int
    edges: int
    finished_at: datetime
    latest: bool = Field(description="false when the blob store did not answer and an older cached build answered")


class NodeLabel(BaseModel):
    level: str
    location: str
    integrity: str


class KgNode(BaseModel):
    id: str
    kind: str
    name: str | None
    status: str
    conf: float | None
    label: NodeLabel
    props: dict = Field(description="the node's properties: path, uri, source, code, line, ...")
    aliases: list[str]


class KindCount(BaseModel):
    kind: str
    count: int


class GraphSummary(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    kinds: list[KindCount] = Field(description="visible nodes by kind, most first")


class NodeSearch(BaseModel):
    graph: GraphRef | None
    query: str
    kinds: list[str] = Field(description="the kinds the search was limited to; empty for all")
    results: list[KgNode]


class Evidence(BaseModel):
    item: str
    anchor: str | None
    rev: str | None
    span: str | None
    uri: str | None
    source: str | None


class Relation(BaseModel):
    rel: str
    node: str
    kind: str
    name: str | None
    status: str
    conf: float | None


class NodeDetail(BaseModel):
    graph: GraphRef
    node: KgNode
    evidence: list[Evidence]
    outgoing: list[Relation] = Field(description="edges from the node, by edge type")
    incoming: list[Relation] = Field(description="edges to the node, by edge type")


class GraphNode(KgNode):
    hop: int = Field(description="steps from the node shown, 0 for the node itself")
    hub: bool = Field(description="left unexpanded: it has more than 150 visible edges")


class GraphEdge(BaseModel):
    id: str
    src: str
    rel: str
    dst: str
    status: str


class Neighbourhood(BaseModel):
    graph: GraphRef
    focus: str = Field(description="the id of the node shown, when it was asked for by an alias")
    hops: int
    limit: int
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    neighbours: int = Field(description="visible direct neighbours of the node")
    left_out: int = Field(description="visible nodes within reach that the node limit left out")
    truncated: bool


# Reading


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the knowledge graph of project {access.name} needs a grant on it")


def _ref(graph: BuiltGraph, latest: BuiltGraph) -> GraphRef:
    return GraphRef(
        build_id=graph.build_id,
        content_hash=graph.content_hash,
        nodes=graph.nodes,
        edges=graph.edges,
        finished_at=graph.finished_at,
        latest=graph.build_id == latest.build_id,
    )


async def _read(request: Request, user, project: str, reader):
    """``reader(session)`` over the project's latest successful build for ``user``, and that build; (None, None)
    when no build succeeded yet."""
    state = request.app.state
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
    if not graphs:
        return None, None
    ceiling = access.rules.grant_label(access.max_level)
    try:
        data, graph = await asyncio.to_thread(
            read, state.kg_graphs, state.blobs, project, access.rules.policy, ceiling, graphs, reader
        )
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except ArtifactMismatch as exc:
        log.error("kg graph artifact refused", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(502, str(exc)) from None
    except GraphProblem as exc:
        log.error("kg graph unreadable", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(503, f"the graph of project {project} cannot be read: {exc}") from None
    return data, _ref(graph, graphs[0])


def _no_graph(project: str) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no graph on the hub yet: push its runs with `evo-agents hub kg push`"
    )


def _hidden(project: str) -> HTTPException:
    return HTTPException(404, f"no node with that id is visible to you in project {project}")


def _text(value) -> str | None:
    return None if value is None else str(value)


@router.get("/{project}/graph", response_model=GraphSummary)
async def graph_summary(request: Request, project: ProjectName, user: CurrentUser) -> GraphSummary:
    """The build the knowledge graph pages read, and how many nodes of each kind the member can see in it."""
    kinds, ref = await _read(request, user, project, WebSession.kinds)
    return GraphSummary(graph=ref, kinds=[KindCount(**k) for k in kinds or []])


@router.get("/{project}/nodes", response_model=NodeSearch)
async def search_nodes(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=200, description="name, id, path, code or words")],
    kind: Annotated[list[str] | None, Query(max_length=20, description="only nodes of these kinds")] = None,
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT)] = SEARCH_LIMIT,
) -> NodeSearch:
    """Visible nodes matching ``q``, best first, as kg_search finds them."""
    kinds = sorted({k for k in kind or [] if k})
    found, ref = await _read(request, user, project, lambda session: session.search(q, kinds, limit))
    results = (found or {}).get("results", [])
    return NodeSearch(graph=ref, query=q, kinds=kinds, results=[KgNode(**n) for n in results])


@router.get("/{project}/node", response_model=NodeDetail)
async def show_node(request: Request, project: ProjectName, node_id: NodeId, user: CurrentUser) -> NodeDetail:
    """One visible node, as kg_node shows it: label, properties, evidence and neighbours by edge type."""

    def reader(session: WebSession):
        try:
            return session.node(node_id)
        except NotVisible:
            return None

    data, ref = await _read(request, user, project, reader)
    if ref is None:
        raise _no_graph(project)
    if data is None:
        raise _hidden(project)
    evidence = [
        Evidence(
            item=str(ev["item"]),
            anchor=_text(ev.get("anchor")),
            rev=_text(ev.get("rev")),
            span=_text(ev.get("span")),
            uri=_text(ev.get("uri")),
            source=_text(ev.get("source")),
        )
        for ev in data["evidence"]
    ]
    return NodeDetail(
        graph=ref,
        node=KgNode(**data["node"]),
        evidence=evidence,
        outgoing=[Relation(**e) for e in data["out"]],
        incoming=[Relation(**e) for e in data["in"]],
    )


@router.get("/{project}/neighbourhood", response_model=Neighbourhood)
async def show_neighbourhood(
    request: Request,
    project: ProjectName,
    node_id: NodeId,
    user: CurrentUser,
    hops: Annotated[int, Query(ge=1, le=MAX_HOPS)] = MAX_HOPS,
    limit: Annotated[int, Query(ge=1, le=MAX_NODES)] = MAX_NODES,
) -> Neighbourhood:
    """The visible nodes and edges within ``hops`` steps of a node, at most ``limit`` nodes, for the graph view."""

    def reader(session: WebSession):
        try:
            return session.neighbourhood(node_id, hops, limit)
        except NotVisible:
            return None

    data, ref = await _read(request, user, project, reader)
    if ref is None:
        raise _no_graph(project)
    if data is None:
        raise _hidden(project)
    return Neighbourhood(
        graph=ref,
        focus=data["focus"],
        hops=data["hops"],
        limit=data["limit"],
        nodes=[GraphNode(**n) for n in data["nodes"]],
        edges=[GraphEdge(**e) for e in data["edges"]],
        neighbours=data["neighbours"],
        left_out=data["left_out"],
        truncated=data["truncated"],
    )
