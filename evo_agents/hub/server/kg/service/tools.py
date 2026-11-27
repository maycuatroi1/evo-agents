"""The kg_* tools of ``evo-agents kg serve`` on the hub, and the other reads of a project's latest graph with the
tools' read rule (``evo_agents.hub.server.kg``, Reading): ``tool_result``, ``unknown_refs``, ``visible_nodes`` and
``delivery_evidence``."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_branches, kg_named_graphs, kg_pool, kg_settings
from evo_agents.hub.kg_build import no_graph_reason
from evo_agents.hub.kg_graph import BuiltGraph, GraphUnavailable, UnknownRefs, answer, mentioning, node_kinds, unknown
from evo_agents.hub.kg_snapshots import Held, requests_waiting
from evo_agents.hub.server import rankers, run_audience
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.graph import CLI_SINK, GRAPHS_TRIED
from evo_agents.hub.server.kg.repository.graph import built_graphs
from evo_agents.hub.server.kg.service.graph import member
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.kg.graph_scope import DEFAULT_GRAPH
from evo_agents.kg.pacing import pacing
from evo_agents.kg.policy import Label
from evo_agents.kg.serve_view import ToolError
from evo_agents.kg.tools import SIGNATURES


def read_sink(access: ProjectAccess, sink: str) -> str:
    if sink == CLI_SINK and CLI_SINK not in access.rules.policy.sinks and access.rules.hub_sink is not None:
        return access.rules.hub_sink.sink
    return sink


async def readable_graphs(engine: AsyncEngine, user: Principal, project: str):
    """(the member's access, the project's kept graphs ``GRAPHS_TRIED`` at most, the member's audience as the database
    gives it now: empty without a graph), in a transaction of its own. 403 without a grant on the project."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    return access, graphs, audience


async def named_graph(conn: AsyncConnection, project_id: int, arguments) -> dict[str, frozenset[str]] | None:
    """The named graph a kg tool call's ``graph`` names, to its sources: ``{}`` for none or ``default``, None for a
    name the project has no graph of (``evo_agents.hub.kg_named_graphs``). The session checks the argument's type."""
    graph = arguments.get("graph") if isinstance(arguments, dict) else None
    if not isinstance(graph, str) or graph.strip() in ("", DEFAULT_GRAPH):
        return {}
    sources = await kg_named_graphs.sources_of(conn, project_id, graph.strip())
    return None if sources is None else {graph.strip(): sources}


def takes_graph(tool: str) -> bool:
    """Whether kg tool ``tool`` takes a ``graph`` argument: kg_status and kg_more take none, so the default graph of a
    run's system is not put in their arguments."""
    return "graph" in SIGNATURES.get(tool, (set(), set()))[0]


def _names_graph(arguments) -> bool:
    graph = arguments.get("graph") if isinstance(arguments, dict) else None
    return isinstance(graph, str) and bool(graph.strip())


async def snapshot_view(state, project_id: int, graphs: list[BuiltGraph], ceiling: Label | None):
    """(the project's snapshot as ``ceiling``'s audience reads it, the build of ``graphs`` it was made of), for
    ``kg_graph.reading``; None when the app keeps none, or none of a build of ``graphs``."""
    snapshots = getattr(state, "kg_snapshots", None)
    held = None if snapshots is None else await snapshots.ready(project_id, graphs[0].build_id)
    if held is None and snapshots is not None and snapshots.loading(project_id) is not None:
        raise HTTPException(503, snapshots.unready(project_id))
    graph = next((g for g in graphs if held is not None and g.build_id == held.build_id), None)
    if held is None or graph is None:
        return None
    return held.view(ceiling.audience if ceiling is not None else frozenset()), graph


async def on_branch(state, project_id: int, arguments) -> tuple[dict, Held | str | None]:
    """(``arguments`` without ``branch``, the snapshot of the draft branch it names or an error to answer with):
    None for no branch or ``main`` (``evo_agents.hub.kg_branches``). Called with no connection held, as the
    snapshot of a branch can take minutes to make."""
    if not isinstance(arguments, dict) or "branch" not in arguments:
        return arguments, None
    arguments = dict(arguments)
    name = arguments.pop("branch")
    if name is None or (isinstance(name, str) and name.strip() in ("", "main")):
        return arguments, None
    snapshots = getattr(state, "kg_snapshots", None)
    if not isinstance(name, str) or snapshots is None:
        return arguments, "error: branch takes the name of a draft branch of the graph"
    try:
        return arguments, await snapshots.branch(project_id, name.strip())
    except kg_branches.BranchError as exc:
        return arguments, f"error: {exc}: `evo-agents hub kg branch list` lists them"


async def tool_result(
    state,
    user: Principal,
    project: str,
    tool: str,
    arguments: dict,
    sink: str,
    *,
    audience: frozenset | None = None,
    explore: bool = False,
    narrow: Callable[[Label | None], Label | None] | None = None,
    whole: bool = False,
) -> dict:
    """One kg_* tool of ``evo-agents kg serve`` for ``user``, answered from the project's latest successful build
    through ``sink``, holding ``user``'s audience as the database says now, or ``audience`` when given, exploring the
    ranking when ``explore`` (``evo_agents.kg.explore``): the MCP tool result, an error included. ``state`` is the app's. Raises HTTPException: 404 for a project the caller cannot see,
    403 without a grant on it, 503 when no graph can be fetched, 502 for an artifact whose bytes are not the build's.
    A read across projects (``agent_reads``) passes ``narrow``, which lowers the ceiling to what the run's project
    lets in (``cross_labels``), and ``whole`` for the data alone, uncut and not reranked (``kg_graph.WholeSession``)."""
    async with UnitOfWork(state.engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        why = None if graphs else await no_graph_reason(conn, access.project_id, project)
        if graphs and audience is None:
            audience = await reader_audience(conn, user)
        tuning = await kg_settings.read(conn, access.project_id) if graphs else None
        named = await named_graph(conn, access.project_id, arguments) if graphs else {}
        run_id = user.acts_for_run
        if (
            graphs
            and named == {}
            and narrow is None
            and run_id is not None
            and isinstance(arguments, dict)
            and not _names_graph(arguments)
            and takes_graph(tool)
        ):
            default = await system_traces.default_graph(conn, run_id, access.project_id)
            if default is not None:  # the graphs its agent uses in its system (uses_kg)
                arguments, named = {**arguments, "graph": default[0]}, {default[0]: default[1]}
    arguments, branch = await on_branch(state, access.project_id, arguments) if graphs else (arguments, None)
    if not graphs:
        return {"content": [{"type": "text", "text": f"error: {why}"}], "isError": True}
    if named is None:
        message = (
            f"error: project {project} has no knowledge graph named {arguments.get('graph')!r}: "
            f"`evo-agents hub kg graph list --project {project}` lists them; {DEFAULT_GRAPH!r} reads every source"
        )
        return {"content": [{"type": "text", "text": message}], "isError": True}
    if isinstance(branch, str):
        return {"content": [{"type": "text", "text": branch}], "isError": True}
    rules = access.rules
    assert audience is not None  # read above, as there are graphs
    ceiling = rules.ceiling(access.max_level, read_sink(access, sink), audience)
    if narrow is not None:
        ceiling = narrow(ceiling)
    if branch is not None:
        snapshot = branch.view(ceiling.audience if ceiling is not None else frozenset()), graphs[0]
    else:
        snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    try:
        with pacing(busy=requests_waiting):
            result = await kg_pool.run(
                answer,
                state.kg_handles,
                (user.user_id, project),
                project,
                rules.policy,
                ceiling,
                sink,
                graphs,
                tool,
                arguments,
                explore,
                tuning,
                whole,
                named,
                snapshot,
            )
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    if run_id is not None and narrow is None:
        async with UnitOfWork(state.engine) as conn:
            graph = arguments.get("graph") if isinstance(arguments, dict) else None
            await system_traces.record_kg(conn, run_id, tool, graph if isinstance(graph, str) else None, result)
    if whole:
        return result
    if tool == "kg_search" and isinstance(arguments.get("query"), str):
        result = await rankers.rerank_kg(state, access, arguments["query"], result)  # plugins' rankers (their point)
    scope = user.scope
    shown = run_audience.revealed(result) if scope is not None else frozenset()
    if scope is not None and shown:  # noted before the result goes back, so a memory the agent writes after reading
        async with UnitOfWork(state.engine) as conn:  # it carries it
            await run_audience.note(conn, scope.run_id, shown)
    return result


async def _graphs_or_why(engine: AsyncEngine, user: Principal, project: str):
    """(the access, the kept graphs, the audience, None) of ``readable_graphs``, or (None, [], frozenset(), why) when
    the member holds no grant on the project."""
    async with UnitOfWork(engine) as conn:
        try:
            access = await project_access(conn, user, project)
            member(access)
        except HTTPException as exc:
            return None, [], frozenset(), str(exc.detail)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    return access, graphs, audience, None


async def unknown_refs(
    state, user: Principal, project: str, sink: str | None, ids: list[str], paths: list[str]
) -> UnknownRefs:
    """Which of node ``ids`` and ``paths`` (``repo:path``, or a path alone) the project's latest successful build
    lacks, as ``user`` reads it through ``sink`` (the project's hub sink when None) with ``tool_result``'s rule, and the
    author emails of the newest commits of the files they name (``kg_graph.HubSession.unknown``). Never raises for the
    graph: when it cannot look them up (no grant, no graph yet, none readable now) every one counts as lacking, and
    ``why`` says so."""
    if not ids and not paths:
        return UnknownRefs((), ())
    every = (tuple(ids), tuple(paths))
    access, graphs, audience, why = await _graphs_or_why(state.engine, user, project)
    if access is None:
        return UnknownRefs(*every, why)
    if not graphs:
        return UnknownRefs(*every, f"project {project} has no knowledge graph on the hub yet")
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else CLI_SINK)
    ceiling = access.rules.ceiling(access.max_level, read_sink(access, through), audience)
    snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    looked = (
        project,
        access.rules.policy,
        ceiling,
        through,
        graphs,
        ids,
        paths,
        snapshot,
    )
    try:
        return await kg_pool.run(unknown, *looked)
    except GraphUnavailable as exc:
        return UnknownRefs(*every, str(exc))


async def visible_nodes(state, access: ProjectAccess, user: Principal, project: str, sink: str, ids: list[str]):
    """Of node ``ids``, those the project's latest successful build holds as ``user`` reads it through ``sink``
    (``unknown_refs``' rule): each to (its stored id, its kind). HTTPException 409 when the graph cannot be read at all
    (none yet, none readable now, nothing of it visible through ``sink``), 502 for an artifact that does not match."""
    async with UnitOfWork(state.engine) as conn:
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    if not graphs:
        raise HTTPException(409, f"project {project} has no knowledge graph on the hub yet: build it first")
    ceiling = access.rules.ceiling(access.max_level, read_sink(access, sink), audience)
    snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    looked = (project, access.rules.policy, ceiling, sink, graphs, ids, snapshot)
    try:
        return await kg_pool.run(node_kinds, *looked)
    except (GraphUnavailable, ToolError) as exc:
        raise HTTPException(409, str(exc)) from None


async def delivery_evidence(
    state, user: Principal, project: str, codes: list[str]
) -> tuple[dict[str, list] | None, str | None]:
    """(what the project's latest readable graph shows of the delivery of each use case code of ``codes``, why not):
    ``mentioning`` as ``user`` reads it through the project's hub sink (``evo_agents.hub.kg_graph.HubSession.mentions``),
    for the baseline of a goal. Never raises for the graph: with no grant, no graph yet, none readable now or a graph
    of an older schema, the evidence is None and the reason says so."""
    if not codes:
        return {}, None
    access, graphs, audience, why = await _graphs_or_why(state.engine, user, project)
    if access is None:
        return None, why
    if not graphs:
        return None, f"project {project} has no knowledge graph on the hub yet"
    through = access.rules.hub_sink.sink if access.rules.hub_sink else CLI_SINK
    ceiling = access.rules.ceiling(access.max_level, read_sink(access, through), audience)
    snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    looked = (project, access.rules.policy, ceiling, through, graphs, codes, snapshot)
    try:
        return await kg_pool.run(mentioning, *looked), None
    except (GraphUnavailable, ToolError) as exc:
        return None, str(exc)
