"""Read-only routes of a project's knowledge graph for the hub's web pages.

- GET /v1/kg/{project}/graph: the build the reads answer from, how many visible nodes of each kind it holds, and the
  commit each git source the member can see read, with how far it was behind its remote's default branch.
- GET /v1/kg/{project}/nodes?q=&kind=&limit=: search, as kg_search.
- GET /v1/kg/{project}/node?id=: one node with its label, evidence (source, revision, span, uri) and its visible
  neighbours by edge type, as kg_node.
- GET /v1/kg/{project}/neighbourhood?id=&hops=&limit=&group=&open=: the nodes and edges the graph view draws, at
  most 2 steps from the node and at most 150 nodes, saying when it left nodes out. The node's direct neighbours come in
  groups by edge type, direction and kind; with ``group``, each group shows at most that many, those with the most
  visible edges first, except the groups ``open`` names, and the second step goes on from those alone.
- GET /v1/kg/{project}/shape: the graph's shape, how many visible edges join each (source kind, edge type, target
  kind); an edge counts only when it and both its nodes are visible.
- GET /v1/kg/{project}/entity-graph?limit=: the entities the entities step stated that the most visible documents
  mention (150 by default, at most 1,000) and the visible relations between them (``evo_agents.hub.kg_entity_graph``).
- GET /v1/kg/{project}/entity-graph/around?id=&depth=&limit=: the entities within 1 to 3 relations of one. Both
  count each entity's code (``code``) and with ``code=true`` add the visible Directories, Files and Symbols the
  entities shown point to (``code_nodes``, ``code_edges``, at most 500).
- GET /v1/kg/{project}/entity?id= and /relation?id=: one entity or relation with the documents it was read from,
  each with the model's quote, its line and a link to the document; an entity also with its code (``code``), each
  code node with the reason it is linked, its confidence and its link at the commit its source read.

The build status the pages show comes from GET /v1/kg/{project}/builds (``evo_agents.hub.server.kg``). Nothing here
writes, queues a build or goes through /mcp.

Every route needs a grant on the project, as the kg_* tools do (404 for a project the caller cannot see, 403 for a
hub admin without a grant), and reads with the grant's max level alone (``ProjectRules.grant_label``, the label of
the hub's one grant-only read rule, ``visible_by_grant`` in ``evo_agents.hub.access``): the web shows the member's
own data in their browser, so no sink's clearance applies. The grant holds the member's audience as the database gives
it at this read (``evo_agents.hub.server.kg_audience``), so the chats they are in now. Visibility itself is the tools'
one filter (``evo_agents.hub.kg_web``). A node that is missing, a node above the member's level and a node of a chat
they are not in all answer 404; the counts by kind are of visible nodes, and the nodes and edges of ``graph`` leave out
those of an audience the member does not hold.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from evo_agents.hub import kg_named_graphs
from evo_agents.hub.kg_entity_graph import (
    CITATIONS,
    CODE_LINKS,
    MAX_CODE_NODES,
    MAX_DEPTH,
    MAX_ENTITY_NODES,
    TOP_NODES,
)
from evo_agents.hub.kg_graph import ArtifactMismatch, BuiltGraph, GraphOutdated, GraphUnavailable
from evo_agents.hub.kg_web import MAX_HOPS, MAX_NODES, SEARCH_LIMIT, GraphProblem, NotVisible, WebSession, read
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import built_graphs, rebuild_outdated, snapshot_view
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.graph_scope import DEFAULT_GRAPH, GraphScope

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


class SourceRead(BaseModel):
    source: str = Field(description="the source's id in knowledge.yaml")
    commit: str = Field(description="the SHA of the commit the source read")
    ref: str | None = Field(description="the ref the source names, HEAD when it names none")
    base: str | None = Field(description="the remote's default branch the commit is compared with, e.g. origin/main")
    behind: int | None = Field(
        description="commits of base the commit read lacks, as the machine that synced it counted; null when unknown"
    )
    url: str | None = Field(description="the commit on its forge, when the repo's origin is on one")


class GraphSummary(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    kinds: list[KindCount] = Field(description="visible nodes by kind, most first")
    sources: list[SourceRead] = Field(
        default_factory=list, description="the commit each git source the member can see read, by source"
    )


class NodeSearch(BaseModel):
    graph: GraphRef | None
    query: str
    kinds: list[str] = Field(description="the kinds the search was limited to; empty for all")
    results: list[KgNode]
    named_graph: str | None = Field(
        None, description="the named graph the search was limited to; null for every source"
    )


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
    degree: int = Field(description="the node's visible edges in the whole graph")


class GraphEdge(BaseModel):
    id: str
    src: str
    rel: str
    dst: str
    status: str


class NeighbourGroup(BaseModel):
    key: str = Field(description="direction:rel:kind, as the open parameter names the group")
    rel: str
    direction: Literal["out", "in"]
    kind: str = Field(description="the kind of the neighbours")
    total: int = Field(description="visible direct neighbours in the group")
    shown: list[str] = Field(description="the ids of those in nodes, most visible edges first")


class Neighbourhood(BaseModel):
    graph: GraphRef
    focus: str = Field(description="the id of the node shown, when it was asked for by an alias")
    hops: int
    limit: int
    group: int | None = Field(description="direct neighbours shown of each group not opened; null for all")
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    groups: list[NeighbourGroup] = Field(description="the node's direct neighbours by edge type, direction and kind")
    neighbours: int = Field(description="visible direct neighbours of the node")
    left_out: int = Field(description="visible nodes within reach that the node limit left out")
    truncated: bool


class ShapeRow(BaseModel):
    src_kind: str
    rel: str
    dst_kind: str
    count: int


class GraphShape(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    shape: list[ShapeRow] = Field(description="visible edges by source kind, edge type and target kind, most first")


# Reading


def require_reader(access: ProjectAccess) -> None:
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


async def read_graph(request: Request, user, project: str, reader):
    """``reader(session)`` over the project's latest successful build for ``user``, and that build; (None, None)
    when no build succeeded yet."""
    state = request.app.state
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    if not graphs:
        return None, None
    ceiling = access.rules.grant_label(access.max_level, audience)
    snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    try:
        data, graph = await asyncio.to_thread(
            read, state.kg_graphs, state.blobs, project, access.rules.policy, ceiling, graphs, reader, snapshot
        )
    except GraphOutdated as exc:
        await rebuild_outdated(state, access.project_id, project)
        raise HTTPException(503, str(exc)) from None
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except ArtifactMismatch as exc:
        log.error("kg graph artifact refused", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(502, str(exc)) from None
    except GraphProblem as exc:
        log.error("kg graph unreadable", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(503, f"the graph of project {project} cannot be read: {exc}") from None
    return data, _ref(graph, graphs[0])


def no_graph(project: str) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no graph on the hub yet: push its runs with `evo-agents hub kg push`"
    )


def _hidden(project: str) -> HTTPException:
    return HTTPException(404, f"no node with that id is visible to you in project {project}")


def _text(value) -> str | None:
    return None if value is None else str(value)


@router.get("/{project}/graph", response_model=GraphSummary)
async def graph_summary(request: Request, project: ProjectName, user: CurrentUser) -> GraphSummary:
    """The build the knowledge graph pages read, how many nodes of each kind the member can see in it, and the commit
    each git source the member can see read."""
    summary, ref = await read_graph(request, user, project, WebSession.summary)
    summary = summary or {}
    return GraphSummary(
        graph=ref,
        kinds=[KindCount(**k) for k in summary.get("kinds", [])],
        sources=[SourceRead(**s) for s in summary.get("sources", [])],
    )


@router.get("/{project}/shape", response_model=GraphShape)
async def graph_shape(request: Request, project: ProjectName, user: CurrentUser) -> GraphShape:
    """How many edges the member can see join each kind of node to each other, by edge type, most first."""
    shape, ref = await read_graph(request, user, project, WebSession.shape)
    return GraphShape(graph=ref, shape=[ShapeRow(**row) for row in shape or []])


@router.get("/{project}/nodes", response_model=NodeSearch)
async def search_nodes(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=200, description="name, id, path, code or words")],
    kind: Annotated[list[str] | None, Query(max_length=20, description="only nodes of these kinds")] = None,
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT)] = SEARCH_LIMIT,
    graph: Annotated[
        str | None,
        Query(min_length=1, max_length=63, description="a named graph of the project: only its sources' nodes"),
    ] = None,
) -> NodeSearch:
    """Visible nodes matching ``q``, best first, as kg_search finds them; of the named graph ``graph`` only when
    given (``evo_agents.hub.server.kg_graphs``)."""
    kinds = sorted({k for k in kind or [] if k})
    scope = await _named_scope(request, user, project, graph)
    found, ref = await read_graph(request, user, project, lambda session: session.search(q, kinds, limit, scope))
    results = (found or {}).get("results", [])
    named = scope.name if scope is not None else None
    return NodeSearch(graph=ref, query=q, kinds=kinds, results=[KgNode(**n) for n in results], named_graph=named)


async def _named_scope(request: Request, user, project: str, graph: str | None) -> GraphScope | None:
    """The scope of the named graph ``graph`` of the project; None for none or ``default``; 404 for a name the
    project has no graph of."""
    if graph is None or graph == DEFAULT_GRAPH:
        return None
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        sources = await kg_named_graphs.sources_of(conn, access.project_id, graph)
    if sources is None:
        raise HTTPException(404, f"project {project} has no knowledge graph named {graph}")
    return GraphScope(graph, sources)


@router.get("/{project}/node", response_model=NodeDetail)
async def show_node(request: Request, project: ProjectName, node_id: NodeId, user: CurrentUser) -> NodeDetail:
    """One visible node, as kg_node shows it: label, properties, evidence and neighbours by edge type."""

    def reader(session: WebSession):
        try:
            return session.node(node_id)
        except NotVisible:
            return None

    data, ref = await read_graph(request, user, project, reader)
    if ref is None:
        raise no_graph(project)
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
    group: Annotated[
        int | None, Query(ge=1, le=MAX_NODES, description="direct neighbours shown of each group; all when absent")
    ] = None,
    opened: Annotated[
        list[str] | None,
        Query(alias="open", max_length=100, description="keys of the groups shown whole (direction:rel:kind)"),
    ] = None,
) -> Neighbourhood:
    """The visible nodes and edges within ``hops`` steps of a node, at most ``limit`` nodes, for the graph view."""
    keys = frozenset(opened or [])

    def reader(session: WebSession):
        try:
            return session.neighbourhood(node_id, hops, limit, group, keys)
        except NotVisible:
            return None

    data, ref = await read_graph(request, user, project, reader)
    if ref is None:
        raise no_graph(project)
    if data is None:
        raise _hidden(project)
    return Neighbourhood(
        graph=ref,
        focus=data["focus"],
        hops=data["hops"],
        limit=data["limit"],
        group=data["group"],
        nodes=[GraphNode(**n) for n in data["nodes"]],
        edges=[GraphEdge(**e) for e in data["edges"]],
        groups=[NeighbourGroup(**g) for g in data["groups"]],
        neighbours=data["neighbours"],
        left_out=data["left_out"],
        truncated=data["truncated"],
    )


# The entity graph (``evo_agents.hub.kg_entity_graph``)

EntityId = Annotated[str, Query(alias="id", min_length=1, max_length=1000, description="an entity's id or alias")]
EntityLimit = Annotated[
    int, Query(ge=1, le=MAX_ENTITY_NODES, description=f"most entities shown, at most {MAX_ENTITY_NODES}")
]
WithCode = Annotated[
    bool,
    Query(description=f"also the code nodes the entities shown point to, at most {MAX_CODE_NODES}"),
]


class EntityNode(BaseModel):
    id: str
    kind: str
    name: str
    type: str = Field(description="the model's type of the entity, its kind when it gave none")
    description: str | None = Field(description="the model's summary of what the documents say of it, else their one")
    docs: int = Field(description="visible documents that mention it")
    degree: int = Field(description="its visible relations to another visible entity")
    code: int = Field(
        default=0, description="visible Directories, Files and Symbols it points to (implemented_by, refers_to)"
    )
    status: str
    conf: float | None
    model: str | None = Field(description="the model that stated it")
    label: NodeLabel


class EntityGraphNode(EntityNode):
    hop: int | None = Field(default=None, description="relations from the entity asked for; null in the overview")


class EntityEdge(BaseModel):
    id: str
    src: str
    rel: str = Field(description="the relation's type: relates_to for an open relation, else the ontology's")
    dst: str
    keywords: str | None
    description: str | None
    weight: float = Field(description="the sum of the strengths read, else the one strength, else 1.0")
    conf: float | None
    status: str
    model: str | None
    sources: int | None = Field(description="statements of the relation the weight sums, when more than one")
    label: NodeLabel


class CodeNode(BaseModel):
    id: str
    kind: str = Field(description="Directory, File or Symbol")
    name: str
    repo: str | None
    path: str | None
    package: str = Field(description="the directory it is in (a Directory's own path), empty at the repo's root")
    entities: int = Field(description="visible entities that point to it")
    label: NodeLabel


class CodeEdge(BaseModel):
    src: str = Field(description="the entity")
    rel: str = Field(description="implemented_by (an agent's entity map) or refers_to (the entity's name names it)")
    dst: str = Field(description="the code node")
    conf: float = Field(description="the surest confidence of the links between the two")


class CodeLink(BaseModel):
    id: str = Field(description="the id of the code node, for its page of the graph")
    kind: str = Field(description="Directory, File or Symbol")
    name: str
    repo: str | None
    path: str | None
    package: str = Field(description="the directory it is in (a Directory's own path), empty at the repo's root")
    label: NodeLabel
    rels: list[str] = Field(description="the relations that link the entity to it: implemented_by, refers_to")
    reason: str | None = Field(description="why the agent that mapped the entity says this code implements it")
    name_match: str | None = Field(description="the entity's name that names this code, for refers_to")
    conf: float | None
    status: str
    model: str | None = Field(description="the model or agent that linked them")
    uri: str | None = Field(description="the code at the commit its source read, when it has a link")
    line: int | None = Field(description="the line a Symbol starts on in that file")


class EntityGraph(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    limit: int
    nodes: list[EntityGraphNode] = Field(description="the entities the most documents mention, most first")
    edges: list[EntityEdge] = Field(description="the visible relations between two of them")
    total_nodes: int = Field(description="visible entities of the graph")
    total_edges: int = Field(description="visible relations of the graph")
    truncated: bool
    code_nodes: list[CodeNode] = Field(default_factory=list, description="with code=true, the code they point to")
    code_edges: list[CodeEdge] = Field(default_factory=list, description="with code=true, entity to code")
    code_left_out: int = Field(default=0, description=f"code nodes past the first {MAX_CODE_NODES} left out")


class EntityNeighbourhood(BaseModel):
    graph: GraphRef
    focus: str = Field(description="the id of the entity shown, when it was asked for by an alias")
    depth: int
    limit: int
    nodes: list[EntityGraphNode]
    edges: list[EntityEdge]
    left_out: int = Field(description="visible entities within reach that the limit left out")
    truncated: bool
    code_nodes: list[CodeNode] = Field(default_factory=list, description="with code=true, the code they point to")
    code_edges: list[CodeEdge] = Field(default_factory=list, description="with code=true, entity to code")
    code_left_out: int = Field(default=0, description=f"code nodes past the first {MAX_CODE_NODES} left out")


class Citation(BaseModel):
    document: str = Field(description="the id of the document node")
    name: str
    path: str | None
    uri: str | None = Field(description="the document at the commit its source read, when it has a link")
    quote: str | None = Field(description="the model's quote of the passage")
    line: int | None = Field(description="the line of the document the quote starts on")
    description: str | None
    model: str | None


class EntityDetail(BaseModel):
    graph: GraphRef
    node: EntityNode
    aliases: list[str]
    citations: list[Citation] = Field(description=f"the documents that mention it, by name, at most {CITATIONS}")
    documents: int = Field(description="visible documents that mention it")
    relations: list[EntityEdge] = Field(description="its visible relations, heaviest first")
    neighbours: list[EntityNode] = Field(description="the entities at the other end of those relations")
    code: list[CodeLink] = Field(
        default_factory=list, description=f"the code it points to, by repo, package and path, at most {CODE_LINKS}"
    )
    code_links: int = Field(default=0, description="visible Directories, Files and Symbols it points to")


class RelationDetail(BaseModel):
    graph: GraphRef
    edge: EntityEdge
    src: EntityNode
    dst: EntityNode
    citations: list[Citation] = Field(description=f"the documents it was read from, at most {CITATIONS}")
    documents: int = Field(description="visible documents it was read from")


@router.get("/{project}/entity-graph", response_model=EntityGraph)
async def entity_graph(
    request: Request, project: ProjectName, user: CurrentUser, limit: EntityLimit = TOP_NODES, code: WithCode = False
) -> EntityGraph:
    """The visible entities the most documents mention and the visible relations between them; with ``code``, the
    visible code nodes they point to."""
    data, ref = await read_graph(request, user, project, lambda session: session.entities(limit, code))
    data = data or {"nodes": [], "edges": [], "total_nodes": 0, "total_edges": 0, "truncated": False}
    return EntityGraph(
        graph=ref,
        limit=limit,
        nodes=[EntityGraphNode(**n) for n in data["nodes"]],
        edges=[EntityEdge(**e) for e in data["edges"]],
        total_nodes=data["total_nodes"],
        total_edges=data["total_edges"],
        truncated=data["truncated"],
        **_code_graph(data),
    )


def _code_graph(data: dict) -> dict:
    return {
        "code_nodes": [CodeNode(**n) for n in data.get("code_nodes", ())],
        "code_edges": [CodeEdge(**e) for e in data.get("code_edges", ())],
        "code_left_out": data.get("code_left_out", 0),
    }


async def _entity_read(request: Request, user, project: str, reader):
    def guarded(session: WebSession):
        try:
            return reader(session)
        except NotVisible:
            return None

    data, ref = await read_graph(request, user, project, guarded)
    if ref is None:
        raise no_graph(project)
    if data is None:
        raise HTTPException(404, f"no entity or relation with that id is visible to you in project {project}")
    return data, ref


@router.get("/{project}/entity-graph/around", response_model=EntityNeighbourhood)
async def entity_neighbourhood(
    request: Request,
    project: ProjectName,
    entity_id: EntityId,
    user: CurrentUser,
    depth: Annotated[int, Query(ge=1, le=MAX_DEPTH)] = 1,
    limit: EntityLimit = TOP_NODES,
    code: WithCode = False,
) -> EntityNeighbourhood:
    """The visible entities within ``depth`` relations of one, at most ``limit``, and the relations between them; with
    ``code``, the visible code nodes they point to."""
    data, ref = await _entity_read(request, user, project, lambda s: s.entities_around(entity_id, depth, limit, code))
    return EntityNeighbourhood(
        graph=ref,
        focus=data["focus"],
        depth=data["depth"],
        limit=data["limit"],
        nodes=[EntityGraphNode(**n) for n in data["nodes"]],
        edges=[EntityEdge(**e) for e in data["edges"]],
        left_out=data["left_out"],
        truncated=data["truncated"],
        **_code_graph(data),
    )


@router.get("/{project}/entity", response_model=EntityDetail)
async def show_entity(request: Request, project: ProjectName, entity_id: EntityId, user: CurrentUser) -> EntityDetail:
    """One visible entity with the documents that mention it, quoted, its relations and the code it points to."""
    data, ref = await _entity_read(request, user, project, lambda s: s.entity(entity_id))
    return EntityDetail(
        graph=ref,
        node=EntityNode(**data["node"]),
        aliases=data["aliases"],
        citations=[Citation(**c) for c in data["citations"]],
        documents=data["documents"],
        relations=[EntityEdge(**e) for e in data["relations"]],
        neighbours=[EntityNode(**n) for n in data["neighbours"]],
        code=[CodeLink(**c) for c in data["code"]],
        code_links=data["code_links"],
    )


@router.get("/{project}/relation", response_model=RelationDetail)
async def show_relation(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    edge_id: Annotated[str, Query(alias="id", min_length=1, max_length=200, description="the relation's edge id")],
) -> RelationDetail:
    """One visible relation between two visible entities, with the documents it was read from."""
    data, ref = await _entity_read(request, user, project, lambda s: s.relation(edge_id))
    return RelationDetail(
        graph=ref,
        edge=EntityEdge(**data["edge"]),
        src=EntityNode(**data["src"]),
        dst=EntityNode(**data["dst"]),
        citations=[Citation(**c) for c in data["citations"]],
        documents=data["documents"],
    )
