"""The proposals of @builder of kind ``system`` (``proposals``; plan multi-agent-systems, criterion a11): where one
applies, how it is checked, applied and rolled back, as its ``Kind``.

A member describes a whole agent system, or a change of one, to @builder in the designer's chat; the agent answers
with a proposal that names the system (``target.system``, its slug) and sets:

- ``spec``: the spec ``evo-agent-system/1`` the system would have, whole (``evo_agents.hub.system_spec``), so the web
  shows the difference from the revision now as faded nodes and edges on the canvas;
- ``agents``: the agent profiles the spec names that the project does not have yet, each as POST .../agents takes one
  (handle, name, description, instructions, runs_on), made at version 1;
- ``graphs``: the named graphs the spec names that the project does not have yet, each a name, a description and the
  sources it narrows the project's build to.

Its version is the system's revision, 0 for a new system. Proposing checks the spec as PUT .../systems/{slug} would,
with the agents and graphs it brings counted as the project's; an admin of the project approving it makes the agents
and the graphs, then writes the spec as the system's next revision, while the system is still at the revision the
proposal saw. Rolling back writes a copy of the revision before as the next one, or deletes a system the proposal
made; the agents and graphs it made stay, since another system may use them by then.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.core_plugins.builder.kinds import Change, Kind, Target, Writing, refuse, said_by_pydantic
from evo_agents.hub import kg_named_graphs, system_spec, tables
from evo_agents.hub.server import agent_systems, audit, system_store
from evo_agents.hub.server.agent_profiles import AgentCreate, create_profile
from evo_agents.hub.server.projects import ProjectAccess

SET_KEYS = ("spec", "agents", "graphs")
AGENT_KEYS = ("handle", "name", "description", "instructions", "runs_on")
GRAPH_KEYS = ("name", "description", "sources")
MAX_NEW = 20  # agents, and named graphs, one proposal makes


def _slug(change: Change) -> str:
    return change.target["system"]


def _agents(given: object, problems: list[str]) -> list[AgentCreate]:
    if given is None:
        return []
    if not isinstance(given, list) or len(given) > MAX_NEW:
        problems.append(f"agents: a list of at most {MAX_NEW} agents")
        return []
    made: list[AgentCreate] = []
    for index, item in enumerate(given):
        extra = sorted(set(item) - set(AGENT_KEYS)) if isinstance(item, dict) else []
        if not isinstance(item, dict) or extra:
            problems.append(f"agents[{index}]: an object of {', '.join(AGENT_KEYS)}")
            continue
        try:
            made.append(AgentCreate.model_validate(item))
        except ValidationError as exc:
            problems.append(f"agents[{index}]: {said_by_pydantic(exc, 'agent')}")
    handles = [agent.handle for agent in made]
    if len(set(handles)) != len(handles):
        problems.append("agents names a handle more than once")
    return made


def _graphs(given: object, problems: list[str]) -> list[dict]:
    if given is None:
        return []
    if not isinstance(given, list) or len(given) > MAX_NEW:
        problems.append(f"graphs: a list of at most {MAX_NEW} named graphs")
        return []
    made: list[dict] = []
    for index, item in enumerate(given):
        extra = sorted(set(item) - set(GRAPH_KEYS)) if isinstance(item, dict) else []
        if not isinstance(item, dict) or extra or not isinstance(item.get("name"), str):
            problems.append(f"graphs[{index}]: an object of {', '.join(GRAPH_KEYS)}, with a name")
            continue
        description = item.get("description") or ""
        try:
            kg_named_graphs.check_name(item["name"])
            sources = kg_named_graphs.check_sources(list(item.get("sources") or []))
        except (kg_named_graphs.GraphError, TypeError) as exc:
            problems.append(f"graphs[{index}]: {exc}")
            continue
        if not isinstance(description, str) or len(description) > kg_named_graphs.MAX_DESCRIPTION_CHARS:
            problems.append(f"graphs[{index}].description: at most {kg_named_graphs.MAX_DESCRIPTION_CHARS} characters")
            continue
        made.append({"name": item["name"], "description": description, "sources": sources})
    names = [graph["name"] for graph in made]
    if len(set(names)) != len(names):
        problems.append("graphs names a graph more than once")
    return made


def parts(change: Change) -> tuple[dict, list[AgentCreate], list[dict]]:
    """The spec, the agents and the named graphs ``change`` sets; ProposalError (422) naming what is wrong with their
    shape."""
    problems = [
        f"{name}: a system proposal sets spec, agents and graphs alone"
        for name in sorted(change.sets)
        if name not in SET_KEYS
    ]
    if change.unset:
        problems.append("a system proposal unsets nothing: set the spec whole")
    spec = change.sets.get("spec")
    if not isinstance(spec, dict):
        problems.append("spec: the spec evo-agent-system/1 the system would have, whole")
        spec = {}
    given = spec.get("slug")
    if given is not None and given != _slug(change):
        problems.append(f"spec.slug is {given!r} and target.system {_slug(change)!r}: they name the same system")
    agents = _agents(change.sets.get("agents"), problems)
    graphs = _graphs(change.sets.get("graphs"), problems)
    refuse(problems)
    return {k: v for k, v in spec.items() if k != "slug"}, agents, graphs


def _named(spec: dict) -> tuple[set[str], set[str]]:
    """The handles and the graphs the nodes of ``spec`` name."""
    nodes = spec.get("nodes") if isinstance(spec.get("nodes"), list) else []
    handles = {n.get("profile") for n in nodes if isinstance(n, dict) and n.get("kind") in system_spec.PINNED_KINDS}
    graphs = {n.get("graph") for n in nodes if isinstance(n, dict) and n.get("kind") == "kg"}
    return {h for h in handles if isinstance(h, str)}, {g for g in graphs if isinstance(g, str)}


def _with_new(refs: system_store.Refs, agents: list[AgentCreate], graphs: list[dict]) -> system_store.Refs:
    """``refs`` as they would be once ``agents`` (at version 1) and ``graphs`` are made."""
    profiles = {**refs.profiles, **{a.handle: system_spec.ProfileRef(1) for a in agents}}
    return replace(refs, profiles=profiles, graphs=refs.graphs | {g["name"] for g in graphs})


def _spec_problems(problems: list[system_spec.Problem]) -> list[str]:
    return [f"spec.{p.path}: {p.message}" if p.path else f"spec: {p.message}" for p in system_spec.errors(problems)]


async def _locate(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    """System ``target.system`` as it is now, or a new system of that slug when the project has none."""
    slug = _slug(change)
    held = await system_store.one(conn, access.project_id, slug, lock=lock)
    if held is None:
        return Target(f"a new agent system {slug} of project {access.name}", 0, {})
    return Target(f"agent system {slug} of project {access.name}", held.revision, {"spec": held.spec})


async def _taken(conn: AsyncConnection, handles: list[str]) -> set[str]:
    """Those of ``handles`` an agent of the hub, a deleted one included, or a project has already."""
    if not handles:
        return set()
    ap, p = tables.agent_profiles, tables.projects
    agents = set((await conn.execute(select(ap.c.handle).where(ap.c.handle.in_(handles)))).scalars())
    return agents | set((await conn.execute(select(p.c.name).where(p.c.name.in_(handles)))).scalars())


async def _check(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    spec_in, agents, graphs = parts(change)
    refs = await system_store.refs(conn, access.project_id)
    handles, kgs = _named(spec_in)
    problems = [
        f"agents: @{handle} is taken on this hub already: name it in the spec, or pick another handle"
        for handle in sorted(await _taken(conn, [a.handle for a in agents]))
    ]
    problems += [f"agents: no node of the spec names @{a.handle}" for a in agents if a.handle not in handles]
    problems += [
        f"graphs: the project has a graph named {g['name']} already" for g in graphs if g["name"] in refs.graphs
    ]
    problems += [f"graphs: no kg node of the spec names {g['name']}" for g in graphs if g["name"] not in kgs]
    if len(refs.graphs) + len(graphs) > kg_named_graphs.MAX_GRAPHS:
        problems.append(f"graphs: a project holds at most {kg_named_graphs.MAX_GRAPHS} named graphs")
    refuse(problems)
    spec, found = agent_systems.checked(spec_in, _with_new(refs, agents, graphs), _slug(change))
    problems = _spec_problems(found)
    if not problems and not agents and not graphs and target.held.get("spec") == spec:
        problems.append(f"{target.title} has this spec already: it changes nothing")
    refuse(problems)


async def _make_graph(w: Writing, graph: dict) -> None:
    try:
        made = await kg_named_graphs.create(
            w.conn, w.access.project_id, graph["name"], graph["sources"], graph["description"], w.user.user_id
        )
    except kg_named_graphs.GraphError as exc:
        status = 409 if isinstance(exc, kg_named_graphs.GraphExists) else 422
        raise ProposalError(status, f"graph {graph['name']}: {exc}; {NOTHING}") from None
    await audit.record(
        w.conn,
        actor_id=w.user.user_id,
        token_id=w.user.token_id,
        action=audit.KG_GRAPH,
        target=f"{w.access.name} graph={made.name} kind=create sources={len(made.sources)} proposal={w.proposal_id}",
        project_id=w.access.project_id,
    )


async def _write(w: Writing, spec_in: dict) -> int:
    """Write ``spec_in``, checked and pinned against the project now, as the system's next revision; that one."""
    slug = _slug(w.change)
    held = await system_store.one(w.conn, w.access.project_id, slug, lock=True)
    refs = await system_store.refs(w.conn, w.access.project_id)
    spec, found = agent_systems.checked(spec_in, refs, slug)
    if problems := _spec_problems(found):
        raise ProposalError(409, f"{'; '.join(problems)}; {NOTHING}")
    if held is not None and held.spec == spec:
        raise ProposalError(409, f"{w.target.title} has this spec already; {NOTHING}")
    return await agent_systems.write_spec(w.conn, w.user, w.access, slug, spec, held, refs)


async def _apply(w: Writing) -> int:
    """Make the agents and the graphs, then write the spec as the system's next revision; that revision."""
    spec_in, agents, graphs = parts(w.change)
    for agent in agents:
        try:
            await create_profile(w.conn, w.user, w.access, agent, w.hub_version)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, f"@{agent.handle}: {exc.detail}") from None
    for graph in graphs:
        await _make_graph(w, graph)
    return await _write(w, spec_in)


async def _roll_back(w: Writing) -> int:
    """The system's next revision, a copy of revision ``applied - 1``; a system the proposal made is deleted (0)."""
    slug, before = _slug(w.change), w.version - 1
    held = await system_store.one(w.conn, w.access.project_id, slug, lock=True)
    assert held is not None  # located at the applied revision
    if before == 0:
        await system_store.remove(w.conn, held.id)
        await agent_systems.record(w.conn, w.user, w.access, audit.SYSTEM_DELETE, slug, held.revision)
        return 0
    spec = await system_store.revision_spec(w.conn, held.id, before)
    if spec is None:
        raise ProposalError(409, f"{w.target.title} has no revision {before}; {NOTHING}")
    return await _write(w, spec)


def described(change: Change, target: Target) -> str:
    """What a system proposal changes on the canvas, in a sentence for its thread."""
    spec = change.sets.get("spec") if isinstance(change.sets.get("spec"), dict) else {}
    before = target.held.get("spec") or {}

    def nodes(of: dict) -> set[str]:
        return {n["id"] for n in of.get("nodes") or () if isinstance(n, dict) and isinstance(n.get("id"), str)}

    def edges(of: dict) -> set[tuple]:
        return {(e.get("from"), e.get("kind"), e.get("to")) for e in of.get("edges") or () if isinstance(e, dict)}

    said = []
    for verb, found in (
        ("adds the nodes", nodes(spec) - nodes(before)),
        ("removes the nodes", nodes(before) - nodes(spec)),
    ):
        if found:
            said.append(f"{verb} {', '.join(sorted(found))}")
    added, removed = len(edges(spec) - edges(before)), len(edges(before) - edges(spec))
    if added or removed:
        said.append(f"adds {added} and removes {removed} edges")
    agents = [a.get("handle") for a in change.sets.get("agents") or () if isinstance(a, dict)]
    graphs = [g.get("name") for g in change.sets.get("graphs") or () if isinstance(g, dict)]
    if agents:
        said.append(f"makes the agents {', '.join(f'@{h}' for h in agents)}")
    if graphs:
        said.append(f"makes the named graphs {', '.join(map(str, graphs))}")
    return f"It {'; '.join(said)}." if said else "It changes the system's names, places or limits."


KIND = Kind(_locate, _check, _apply, _roll_back)
