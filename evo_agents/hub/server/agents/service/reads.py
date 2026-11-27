"""The agent of a chat run reads the knowledge graph and the memories of another project (``evo-agents worker agent
read``, ``chat_runs.READ_COMMAND``): POST /v1/worker/runs/{run}/agent/reads. ``docs/hub.md`` (Agents across projects)
is the design.

The route takes the worker token of the worker that holds a chat run (404 for a run it does not hold, 422 for a run of
another kind or for the run's own project, which the evo-hub tools read). It reads as the person at the origin of the
chain (``chats.service.agents.agent_run``) with that person's grant on the project read, through that project's hub sink, as
the hub holds it; 404 when that person holds no grant there, as for a project that is not registered.

Then the one check of ``cross_labels``: what the hub sink of the run's project does not let in is left out, and the
answer says how many items were and why. kg_search and kg_context read twice: once whole within the person's
clearance, to count what the check leaves out, then within the ceiling the check gives, which is what the agent gets,
cut at the graph's own limit (``kg.tool_result``'s ``narrow`` and ``whole``). memory_search and memory_get read as the
memory routes do and leave out the memories the check refuses; a memory of another project or a personal one is
refused as not found. The audience of what is shown is noted as the run's (``run_audience``), as the evo-hub tools do.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import Request

from evo_agents.hub.server import cross_labels, kg, run_audience
from evo_agents.hub.server.agents.models.reads import CUT, CUT_NOTE, CrossRead
from evo_agents.hub.server.agents.service.shapes import CrossReadIn
from evo_agents.hub.server.base.errors import NotFound, Unprocessable
from evo_agents.hub.server.chats.service.agents import agent_run
from evo_agents.hub.server.mcp_text import memory_about, memory_line
from evo_agents.hub.server.memories.service import core as memories
from evo_agents.hub.server.memories.service.audience import label_audience
from evo_agents.hub.server.projects import ProjectAccess, access_of, not_found, project_access
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


def arguments(body: CrossReadIn) -> dict:
    """The arguments of the kg_* tool, as kg serve takes them; 422 when the tool lacks what it needs."""
    if body.tool == "kg_search":
        if body.query is None:
            raise Unprocessable("kg_search needs a query")
        return {"query": body.query, "limit": body.limit, **_graph(body)}
    if body.query is None and not body.ids:
        raise Unprocessable("kg_context needs a query or node ids")
    found = {"query": body.query} if body.query is not None else {"ids": list(body.ids)}
    return {**found, **_graph(body)}


def _graph(body: CrossReadIn) -> dict:
    """``graph`` of a kg_* read when it names one: a named graph of the project read, which only narrows."""
    return {"graph": body.graph} if body.graph is not None else {}


def elements(data) -> dict[str, Mapping]:
    """The elements of a kg_* tool's data that carry an id and a label: id to label. Properties are skipped, as
    ``kg.sessions.labels_in`` skips them: they hold what a source said."""
    found: dict[str, Mapping] = {}
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            label = current.get("label")
            if isinstance(label, Mapping) and "level" in label and isinstance(current.get("id"), str):
                found.setdefault(current["id"], label)
            stack.extend(value for key, value in current.items() if key not in ("label", "props"))
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _text(result: dict) -> str:
    text = "\n".join(block.get("text", "") for block in result.get("content") or [] if isinstance(block, dict)).rstrip()
    cut = CUT.search(text)
    return text[: cut.start()] + CUT_NOTE if cut else text


async def _kg(state, origin: Principal, gate: cross_labels.Gate, sink: str, body: CrossReadIn):
    """(text, shown, reasons, the result shown or None) of a kg_* read across projects."""
    arguments_of, project = arguments(body), body.project
    whole = await kg.tool_result(state, origin, project, body.tool, arguments_of, sink, whole=True)
    if whole.get("isError"):
        return _text(whole), 0, [], None
    seen = elements(whole.get("structuredContent"))
    reasons = [why for why in (gate.why(label) for label in seen.values()) if why is not None]
    if gate.ceiling is None:
        return "", 0, reasons, None
    ceiling = gate.ceiling

    def narrow(held):
        return None if held is None else held.meet(ceiling)

    result = await kg.tool_result(state, origin, project, body.tool, arguments_of, sink, narrow=narrow)
    data = result.get("structuredContent") or {}
    shown = len(seen) - len(reasons) if data.get("truncated") else len(elements(data))
    return _text(result), 0 if result.get("isError") else shown, reasons, result


async def _memories(request: Request, origin: Principal, gate: cross_labels.Gate, sink: str, body: CrossReadIn):
    """(text, shown, reasons, audiences shown) of a memory read across projects."""
    project = body.project
    if body.tool == "memory_search":
        if body.query is None:
            raise Unprocessable("memory_search needs a query")
        found = await memories.search(
            request.app.state.engine,
            origin,
            q=body.query,
            scope="project",
            project=project,
            location=None,
            limit=body.limit,
            sink=sink,
            agent=None,
        )
        kept, reasons = [], []
        for memory in found.items:
            why = gate.why(memory.label)
            if why is None:
                kept.append(memory)
            else:
                reasons.append(why)
        lines = [line for memory in kept for line in (memory_line(memory), f"    {memory_about(memory.body)}")]
        shown = [label_audience(memory.label) for memory in kept]
        return "\n".join(lines), len(kept), reasons, shown
    if body.memory_id is None:
        raise Unprocessable("memory_get needs memory_id")
    memory = await memories.shown(request.app.state.engine, origin, body.memory_id, sink)
    if memory.project != project:  # a personal memory, or one of another project: not this read's
        raise NotFound(f"no memory {body.memory_id} of project {project} that you can see")
    why = gate.why(memory.label)
    if why is not None:
        return "", 0, [why], []
    updated = f"updated {memory.updated_at:%Y-%m-%d %H:%M} UTC by {memory.updated_by}"
    return f"{memory_line(memory)}, {updated}\n\n{memory.body}", 1, [], [label_audience(memory.label)]


async def _accesses(conn, origin: Principal, own: str, project: str) -> tuple[ProjectAccess, ProjectAccess]:
    """(the run's project's rules, the origin's access to ``project``); 404 when the origin holds no grant there."""
    target = await access_of(conn, None, own)
    source = await project_access(conn, origin, project)
    if target is None or source.role is None:
        raise NotFound(not_found(project))
    return target, source


async def read(request: Request, user: Principal, run_id: int, body: CrossReadIn) -> CrossRead:
    """Read the knowledge graph or the memories of another project, in chat run ``run_id`` the worker of ``user``
    holds, within what the hub sink of the run's project lets in (see the module's docstring)."""
    state = request.app.state
    async with state.engine.begin() as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise Unprocessable(f"run {run_id} is a {run.kind} run: only a chat run's agent reads other projects")
        own = run.profile.project
        if body.project == own:
            raise Unprocessable(
                f"project {own} is this run's own: read it with the evo-hub tools (kg_search, memory_search)"
            )
        target, source = await _accesses(conn, run.principal, own, body.project)
    gate = cross_labels.gate(target.rules, source.rules)
    hub_sink = source.rules.hub_sink
    if hub_sink is None:
        text = f"project {body.project} declares no hub sink, so the hub holds nothing of it to read"
        return CrossRead(project=body.project, tool=body.tool, text=text, shown=0, withheld=0, why=None)
    if body.tool in ("kg_search", "kg_context"):
        text, shown, reasons, result = await _kg(state, run.principal, gate, hub_sink.sink, body)
        audiences = [run_audience.revealed(result)] if result is not None else []
    else:
        text, shown, reasons, audiences = await _memories(request, run.principal, gate, hub_sink.sink, body)
    seen = frozenset().union(*audiences)
    if seen:
        async with state.engine.begin() as conn:
            await run_audience.note(conn, run_id, seen)
    why = cross_labels.withheld(reasons) or None
    if not text:
        text = f"nothing of project {body.project} that this run may read matches"
    if why:
        text = f"{text}\n\n{why}"
    log.info(
        "agent read another project",
        extra={"run_id": run_id, "tool": body.tool, "shown": shown, "withheld": len(reasons)},
    )
    return CrossRead(project=body.project, tool=body.tool, text=text, shown=shown, withheld=len(reasons), why=why)
