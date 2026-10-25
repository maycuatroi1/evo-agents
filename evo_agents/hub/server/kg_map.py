"""Read-only routes of a project's knowledge graph map for the hub's web pages and ``evo-agents hub kg clusters``.

- GET /v1/kg/{project}/clusters?nodes=: the level-1 clusters the member can see, each with its visible size, kinds and
  label, the links between them, and (``nodes``, the default) the visible members with their cluster and the visible
  edges between them, for the map. Empty with a reason when the build has no clusters or none is visible.
- GET /v1/kg/{project}/cluster?id=: one cluster of either level with its subclusters, visible members, the edges
  between them and its neighbouring clusters; 404 when no member of it is visible.
- GET /v1/kg/{project}/whole?limit=: every visible node, at most ``limit``, with the level-1 cluster it counts in, and
  every visible edge between two of them, compact, for the map's view of the whole graph.

Access and visibility are those of the graph's other web reads (``evo_agents.hub.server.kg_web``): a grant on the
project, the grant's ceiling with the member's audience, and every node through ``Session.visible``; sizes, links and
labels are counted again over what the member can see (``evo_agents.hub.kg_map``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from evo_agents.hub.kg_map import WHOLE_NODES
from evo_agents.hub.kg_web import NotVisible, WebSession
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_web import GraphRef, no_graph, read_graph
from evo_agents.hub.server.security import CurrentUser

NO_BUILD = "no build of this project succeeded yet"

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 502, 503)},
)


class ClusterSummary(BaseModel):
    id: str
    level: int = Field(description="1 for a cluster of the whole graph, 2 for a part of a large level-1 cluster")
    parent: str | None = Field(description="the level-1 cluster of a level-2 cluster")
    label: str = Field(description="its words by c-TF-IDF over the names and paths of the members you can see")
    size: int = Field(description="its members you can see")
    kinds: dict[str, int] = Field(description="its members you can see, by kind")
    subclusters: int = Field(description="its level-2 clusters with a member you can see")


class ClusterLink(BaseModel):
    a: str
    b: str
    weight: int = Field(description="the edges you can see joining members of the two clusters")


class MapNode(BaseModel):
    id: str
    kind: str
    name: str | None
    cluster: str = Field(description="its level-1 cluster")
    sub: str | None = Field(description="its level-2 cluster, if any")


class ClusterMap(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    computed: bool = Field(description="false when there is no cluster to show; reason says why")
    reason: str | None
    clusters: list[ClusterSummary] = Field(description="the level-1 clusters you can see, largest first")
    links: list[ClusterLink] = Field(description="level-1 clusters joined by edges you can see, heaviest first")
    nodes: list[MapNode] = Field(description="the members you can see, by id; empty with nodes=false")
    edges: list[list[int]] = Field(
        description="the edges you can see between two members, as [index, index, weight] into nodes"
    )


class ClusterNeighbour(BaseModel):
    id: str
    label: str
    size: int
    weight: int = Field(description="the edges you can see joining it to the cluster")


class ClusterDetail(BaseModel):
    graph: GraphRef
    cluster: ClusterSummary
    subclusters: list[ClusterSummary] = Field(description="its level-2 clusters you can see, largest first")
    members: list[MapNode] = Field(description="its members you can see, by id")
    edges: list[list[int]] = Field(description="the edges between two members, as [index, index, weight] into members")
    neighbours: list[ClusterNeighbour] = Field(description="clusters of its level its edges reach, heaviest first")


class WholeNode(BaseModel):
    id: str
    kind: str
    name: str | None
    cluster: str | None = Field(description="the level-1 cluster it counts in, if one you can see")


class WholeGraph(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    nodes: list[WholeNode] = Field(description="the nodes you can see, by id, at most limit")
    rels: list[str] = Field(description="the edge types edges index")
    edges: list[list[int]] = Field(description="[source index, target index, type index into rels]")
    truncated: bool = Field(description="true when you can see more nodes than limit")


ClusterId = Annotated[str, Query(alias="id", min_length=1, max_length=200, description="a cluster id")]


@router.get("/{project}/clusters", response_model=ClusterMap)
async def cluster_map(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    nodes: Annotated[bool, Query(description="include the members and their edges, for the map")] = True,
) -> ClusterMap:
    """The level-1 clusters of the graph you can see, the links between them and, for the map, their members."""
    data, ref = await read_graph(request, user, project, lambda session: session.cluster_map(nodes))
    if ref is None:
        return ClusterMap(graph=None, computed=False, reason=NO_BUILD, clusters=[], links=[], nodes=[], edges=[])
    return ClusterMap(
        graph=ref,
        computed=data["computed"],
        reason=data["reason"],
        clusters=[ClusterSummary(**c) for c in data["clusters"]],
        links=[ClusterLink(**link) for link in data["links"]],
        nodes=[MapNode(**n) for n in data["nodes"]],
        edges=data["edges"],
    )


@router.get("/{project}/cluster", response_model=ClusterDetail)
async def show_cluster(
    request: Request, project: ProjectName, cluster_id: ClusterId, user: CurrentUser
) -> ClusterDetail:
    """One cluster you can see a member of: its subclusters, members, their edges and its neighbouring clusters."""

    def reader(session: WebSession):
        try:
            return session.cluster(cluster_id)
        except NotVisible:
            return None

    data, ref = await read_graph(request, user, project, reader)
    if ref is None:
        raise no_graph(project)
    if data is None:
        raise HTTPException(404, f"no cluster with that id is visible to you in project {project}")
    return ClusterDetail(
        graph=ref,
        cluster=ClusterSummary(**data["cluster"]),
        subclusters=[ClusterSummary(**c) for c in data["subclusters"]],
        members=[MapNode(**n) for n in data["members"]],
        edges=data["edges"],
        neighbours=[ClusterNeighbour(**n) for n in data["neighbours"]],
    )


@router.get("/{project}/whole", response_model=WholeGraph)
async def whole_graph(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=WHOLE_NODES)] = WHOLE_NODES,
) -> WholeGraph:
    """Every node you can see, at most ``limit``, and every edge you can see between two of them, compact."""
    data, ref = await read_graph(request, user, project, lambda session: session.whole_graph(limit))
    if ref is None:
        return WholeGraph(graph=None, nodes=[], rels=[], edges=[], truncated=False)
    return WholeGraph(
        graph=ref,
        nodes=[WholeNode(**n) for n in data["nodes"]],
        rels=data["rels"],
        edges=data["edges"],
        truncated=data["truncated"],
    )
