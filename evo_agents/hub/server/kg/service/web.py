"""Reading a project's graph for the hub's web pages (``evo_agents.hub.server.kg_web``): who may, the scope of a
named graph, and ``read_graph``, which reads the latest build with the grant's label alone."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import kg_branches, kg_named_graphs, kg_pool, kg_snapshots
from evo_agents.hub.kg_graph import BuiltGraph, GraphUnavailable
from evo_agents.hub.kg_web import GraphProblem, read
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.web import GRAPHS_TRIED, GraphRef
from evo_agents.hub.server.kg.repository.graph import built_graphs
from evo_agents.hub.server.kg.service.tools import snapshot_view
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.kg.graph_scope import DEFAULT_GRAPH, GraphScope
from evo_agents.kg.pacing import pacing

log = logging.getLogger("evo_agents.hub.server.kg_web")


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


async def read_graph(state, user, project: str, reader, branch: str = ""):
    """``reader(session)`` over the project's latest successful build for ``user``, and that build; (None, None)
    when no build succeeded yet. A ``branch`` reads that draft branch of the graph instead of main
    (``evo_agents.hub.kg_branches``); 404 for one the project lacks. ``state`` is the app's."""
    held = None
    async with UnitOfWork(state.engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    if not graphs:
        return None, None
    if branch not in ("", "main"):
        try:
            held = await state.kg_snapshots.branch(access.project_id, branch)
        except kg_branches.BranchError as exc:
            raise HTTPException(exc.status, str(exc)) from None
    ceiling = access.rules.grant_label(access.max_level, audience)
    if held is not None:
        snapshot = held.view(ceiling.audience if ceiling is not None else frozenset()), graphs[0]
    else:
        snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    try:
        with pacing(busy=kg_snapshots.requests_waiting):
            data, graph = await kg_pool.run(read, project, access.rules.policy, ceiling, graphs, reader, snapshot)
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except GraphProblem as exc:
        log.error("kg graph unreadable", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(503, f"the graph of project {project} cannot be read: {exc}") from None
    return data, _ref(graph, graphs[0])


async def named_scope(engine: AsyncEngine, user, project: str, graph: str | None) -> GraphScope | None:
    """The scope of the named graph ``graph`` of the project; None for none or ``default``; 404 for a name the
    project has no graph of."""
    if graph is None or graph == DEFAULT_GRAPH:
        return None
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        sources = await kg_named_graphs.sources_of(conn, access.project_id, graph)
    if sources is None:
        raise HTTPException(404, f"project {project} has no knowledge graph named {graph}")
    return GraphScope(graph, sources)
