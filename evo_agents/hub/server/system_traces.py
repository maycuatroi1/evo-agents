"""Traces of agent systems in the database (``evo_agents.hub.tables_traces``; ``docs/hub.md``, Agent systems): which
system a chat run belongs to, the trace and spans its runs write, the brief of an ask, and the budget of a trace.

A person's message to the entry agent of a system, in any chat (a direct chat on Home included), opens a trace: the
run it hands is the trace's first ``invoke_agent`` span (``join``). A run goes into the trace of the run whose message
woke it: an agent's mention or the question of an ask (``agent_asks``) is a message of the asking run, so the tree of
a trace is the tree of ``spawned_by_run_id`` and of mentions. A run that goes on in a session (a parked run resumed,
an inbox untaken, a reply in the thread) is a span of the same trace, under the same parent, marked as a resume. A run
whose node the spec lacks is an external node (``system_context.external_node``) and its edge is off design; so is
any edge the spec has no interacts edge for, with the reason the ask gave or none for a mention.

The system of a run (``of_run``) is its trace's, at the revision the trace started under; for a run in no trace, the
system whose entry agent it is, else the latest system of a trace in its chat that has its profile, else the first
system of the project that has it (``system_for``). ``system_for`` also gives the version of a profile a new session
pins (``chat_runs._pinned``).

``execute_tool`` spans record what a run does through the hub: an ask (with the brief fields present), a mention, a
forward, a query of a knowledge graph (``gen_ai.data_source.id``), and a refusal. Each write notifies CHANNEL with
the system's id, which wakes the trace streams of that system (``system_trace_views``). Tokens and cost are not
copied into spans: ``run_numbers`` reads them from run_spend when a trace is shown, and ``usage`` sums them over the
trace's runs and the runs their tree spawned, for the budget (``budget_problem``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context, system_spec, tables
from evo_agents.hub.server import system_store
from evo_agents.kg.sessions import READ_META

log = logging.getLogger(__name__)

CHANNEL = "evo_system_traces"  # notified with a system's id when a span of one of its traces is written
RESUME = "evo.resume_of_run_id"  # the attribute of an invoke_agent span of a run that goes on in a session
ENDED_OK = ("done",)  # the end states of a run whose span is ok; the others are errors
MAX_RESUMES = 50  # how far back the brief of a session is looked for
KG_RESULTS = "evo.kg.results"  # the attribute of a kg span: how many results the query read (record_kg)


@dataclass(frozen=True)
class SystemRef:
    """A system as a run sees it: its id (None once deleted), slug, project, the revision and spec it runs under."""

    id: int | None
    slug: str
    project_id: int
    revision: int
    spec: dict


@dataclass(frozen=True)
class Membership:
    """A run in a system: its trace and span (None for a run in no trace), its agent and node (None: the spec lacks
    it)."""

    system: SystemRef
    handle: str
    node: str | None
    trace_id: int | None = None
    span_id: int | None = None
    run_id: int | None = None

    @property
    def spec(self) -> dict:
        return self.system.spec


@dataclass(frozen=True)
class _Span:
    id: int
    trace_id: int
    parent_id: int | None
    node: str | None
    from_node: str | None
    off_design: bool
    reason: str | None
    message_id: int | None


async def notify(conn: AsyncConnection, system_id: int | None) -> None:
    if system_id is not None:
        await conn.execute(select(func.pg_notify(CHANNEL, str(system_id))))


async def _system_row(conn: AsyncConnection, query) -> SystemRef | None:
    s = tables.agent_systems
    row = (await conn.execute(select(s.c.id, s.c.slug, s.c.project_id, s.c.revision, s.c.spec).where(query))).first()
    return None if row is None else SystemRef(row.id, row.slug, row.project_id, row.revision, row.spec)


async def _trace_system(conn: AsyncConnection, trace_id: int) -> SystemRef:
    t = tables.system_traces
    row = (
        await conn.execute(
            select(t.c.system_id, t.c.system_slug, t.c.project_id, t.c.system_revision).where(t.c.id == trace_id)
        )
    ).one()
    spec = None if row.system_id is None else await system_store.revision_spec(conn, row.system_id, row.system_revision)
    return SystemRef(row.system_id, row.system_slug, row.project_id, row.system_revision, spec or {})


async def system_for(
    conn: AsyncConnection, project_id: int, profile_id: int, handle: str, chat_id: int | None
) -> SystemRef | None:
    """The system profile ``profile_id`` (``handle``, of project ``project_id``) runs in outside a trace: the one it is
    the entry agent of; else the latest system of a trace of chat ``chat_id`` whose spec has it; else the first system
    of the project that has it; None for none."""
    s, t = tables.agent_systems, tables.system_traces
    found = await _system_row(conn, s.c.entry_profile_id == profile_id)
    if found is not None:
        return found
    if chat_id is not None:
        query = select(t.c.id).where(t.c.chat_id == chat_id, t.c.system_id.is_not(None)).order_by(t.c.id.desc())
        for trace_id in (await conn.execute(query.limit(20))).scalars():
            held = await _trace_system(conn, trace_id)
            if system_context.node_of(held.spec, handle) is not None:
                return held
    query = select(s.c.id, s.c.slug, s.c.revision, s.c.spec).where(s.c.project_id == project_id).order_by(s.c.id)
    for row in await conn.execute(query):
        if system_context.node_of(row.spec, handle) is not None:
            return SystemRef(row.id, row.slug, project_id, row.revision, row.spec)
    return None


async def _span_of(conn: AsyncConnection, run_id: int | None) -> _Span | None:
    if run_id is None:
        return None
    s = tables.system_spans
    query = select(
        s.c.id, s.c.trace_id, s.c.parent_id, s.c.node, s.c.from_node, s.c.off_design, s.c.reason, s.c.message_id
    ).where(s.c.run_id == run_id, s.c.name == system_context.INVOKE)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _Span(*row)


async def _run_row(conn: AsyncConnection, run_id: int):
    r, ap = tables.runs, tables.agent_profiles
    query = (
        select(
            r.c.profile_id,
            r.c.project_id,
            r.c.runtime,
            r.c.chat_id,
            r.c.chat_message_id,
            r.c.resume_of_run_id,
            ap.c.handle,
        )
        .join_from(r, ap, ap.c.id == r.c.profile_id)
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def of_run(conn: AsyncConnection, run_id: int) -> Membership | None:
    """The system run ``run_id`` is a run of, and its trace and span when it has one; None for a run of no agent of a
    system."""
    row = await _run_row(conn, run_id)
    if row is None:
        return None
    span = await _span_of(conn, run_id)
    if span is not None:
        held = await _trace_system(conn, span.trace_id)
        return Membership(held, row.handle, span.node, span.trace_id, span.id, run_id)
    held = await system_for(conn, row.project_id, row.profile_id, row.handle, row.chat_id)
    if held is None:
        return None
    return Membership(held, row.handle, system_context.node_of(held.spec, row.handle), run_id=run_id)


async def pinned_for(conn: AsyncConnection, profile_id: int, handle: str, project_id: int, message_id: int | None):
    """The system a new session of profile ``profile_id``, woken by message ``message_id``, runs in: the trace's of
    the run that wrote the message when its spec has the profile, else ``system_for``; None for none."""
    m = tables.chat_messages
    row = None
    if message_id is not None:
        row = (await conn.execute(select(m.c.run_id, m.c.chat_id).where(m.c.id == message_id))).one_or_none()
    asker = await _span_of(conn, row.run_id if row is not None else None)
    if asker is not None:
        held = await _trace_system(conn, asker.trace_id)
        if system_context.node_of(held.spec, handle) is not None:
            return held
    return await system_for(conn, project_id, profile_id, handle, row.chat_id if row is not None else None)


def pinned_version(spec: dict, handle: str) -> int | None:
    """The version of agent ``handle`` the spec pins; None when it pins none."""
    for found in system_spec.pins(spec):
        if found["profile"] == handle and found.get("version"):
            return int(found["version"])
    return None


# Joining a trace


async def _open(
    conn: AsyncConnection, held: SystemRef, chat_id: int | None, message_id: int | None, user_id: int
) -> int:
    t = tables.system_traces
    values = {
        "project_id": held.project_id,
        "system_id": held.id,
        "system_slug": held.slug,
        "system_revision": held.revision,
        "chat_id": chat_id,
        "message_id": message_id,
        "opened_by": user_id,
    }
    return (await conn.execute(insert(t).values(**values).returning(t.c.id))).scalar_one()


async def join(conn: AsyncConnection, run_id: int, *, previous_id: int | None = None) -> int | None:
    """Put chat run ``run_id``, just queued, in its trace (see the module's docstring), ``previous_id`` the run it goes
    on from; the trace's id, None when it is in none. A run in a trace already is left as it is."""
    if await _span_of(conn, run_id) is not None:
        return None
    row = await _run_row(conn, run_id)
    if row is None:
        return None
    previous_id = previous_id or row.resume_of_run_id
    m = tables.chat_messages
    message = None
    if row.chat_message_id is not None:
        query = select(m.c.author_user_id, m.c.run_id).where(m.c.id == row.chat_message_id)
        message = (await conn.execute(query)).one_or_none()
    previous = await _span_of(conn, previous_id)
    same_message = previous is not None and previous.message_id == row.chat_message_id
    attributes: dict = {}
    if previous is not None and same_message:
        trace_id, parent, from_node, off, reason = (
            previous.trace_id,
            previous.parent_id,
            previous.from_node,
            previous.off_design,
            previous.reason,
        )
        attributes[RESUME] = previous_id
        held = await _trace_system(conn, trace_id)
    elif (
        message is not None
        and message.author_user_id is not None
        and (entry := await _system_row(conn, tables.agent_systems.c.entry_profile_id == row.profile_id))
    ):
        held = entry
        trace_id = await _open(conn, held, row.chat_id, row.chat_message_id, message.author_user_id)
        parent, from_node, off, reason = None, system_context.entry_node(held.spec), False, None
    elif message is not None and (asker := await _span_of(conn, message.run_id)) is not None:
        trace_id, parent, from_node = asker.trace_id, asker.id, asker.node
        held = await _trace_system(conn, trace_id)
        node = system_context.node_of(held.spec, row.handle)
        off, reason = not system_context.on_design(held.spec, from_node, node), None
    elif previous is not None:
        trace_id, parent, from_node, off, reason = (
            previous.trace_id,
            previous.parent_id,
            previous.from_node,
            previous.off_design,
            previous.reason,
        )
        attributes[RESUME] = previous_id
        held = await _trace_system(conn, trace_id)
    else:
        return None
    node = system_context.node_of(held.spec, row.handle) or system_context.external_node(row.handle)
    attributes.update(
        {
            "gen_ai.operation.name": system_context.INVOKE,
            "gen_ai.agent.name": row.handle,
            "gen_ai.conversation.id": str(row.chat_id) if row.chat_id is not None else None,
        }
    )
    s, t = tables.system_spans, tables.system_traces
    values = {
        "trace_id": trace_id,
        "parent_id": parent,
        "run_id": run_id,
        "project_id": row.project_id,
        "name": system_context.INVOKE,
        "agent": row.handle,
        "node": node,
        "from_node": from_node,
        "runtime": row.runtime,
        "status": "running",
        "off_design": off,
        "reason": reason,
        "message_id": row.chat_message_id,
        "attributes": {key: value for key, value in attributes.items() if value is not None},
    }
    await conn.execute(insert(s).values(**values))
    await conn.execute(update(t).values(last_at=func.now()).where(t.c.id == trace_id))
    await notify(conn, held.id)
    log.info("run joined a trace", extra={"run_id": run_id, "trace_id": trace_id, "off_design": off})
    if RESUME not in attributes:
        from evo_agents.hub.server import orchestrator  # it opens threads through chat_runs, which joins runs here

        await orchestrator.check_loop(conn, run_id)
    return trace_id


async def mark_reason(conn: AsyncConnection, run_id: int, reason: str | None) -> None:
    """Record ``reason`` on the off-design edge into run ``run_id``'s span."""
    if not reason:
        return
    s = tables.system_spans
    await conn.execute(
        update(s)
        .values(reason=reason)
        .where(s.c.run_id == run_id, s.c.name == system_context.INVOKE, s.c.off_design.is_(True))
    )


async def end_run(conn: AsyncConnection, run_id: int, state: str) -> None:
    """End the invoke_agent span of run ``run_id``, which ended in ``state``."""
    s, t = tables.system_spans, tables.system_traces
    status = "ok" if state in ENDED_OK else "error"
    query = (
        update(s)
        .values(status=status, ended_at=func.now())
        .where(s.c.run_id == run_id, s.c.name == system_context.INVOKE, s.c.status == "running")
        .returning(s.c.trace_id)
    )
    trace_id = (await conn.execute(query)).scalar_one_or_none()
    if trace_id is None:
        return
    system_id = (
        await conn.execute(update(t).values(last_at=func.now()).where(t.c.id == trace_id).returning(t.c.system_id))
    ).scalar_one()
    await notify(conn, system_id)


async def record_tool(
    conn: AsyncConnection,
    membership: Membership,
    tool: str,
    *,
    to_handle: str | None = None,
    status: str = "ok",
    off_design: bool = False,
    reason: str | None = None,
    message_id: int | None = None,
    attributes: dict | None = None,
) -> int | None:
    """An execute_tool span of tool ``tool`` under the span of ``membership``'s run, toward agent ``to_handle`` when it
    names one; its id, None for a run in no trace."""
    if membership.trace_id is None:
        return None
    to_node = None
    if to_handle is not None:
        to_node = system_context.node_of(membership.spec, to_handle) or system_context.external_node(to_handle)
    s, t = tables.system_spans, tables.system_traces
    project_id = (await conn.execute(select(s.c.project_id).where(s.c.id == membership.span_id))).scalar_one_or_none()
    values = {
        "trace_id": membership.trace_id,
        "parent_id": membership.span_id,
        "run_id": membership.run_id,
        "project_id": project_id,
        "name": system_context.TOOL,
        "tool": tool,
        "agent": membership.handle,
        "node": membership.node,
        "from_node": membership.node,
        "to_node": to_node,
        "status": status,
        "off_design": off_design,
        "reason": reason,
        "message_id": message_id,
        "attributes": {
            "gen_ai.operation.name": system_context.TOOL,
            "gen_ai.tool.name": tool,
            "gen_ai.agent.name": membership.handle,
            **(attributes or {}),
        },
        "ended_at": func.now(),
    }
    span_id = (await conn.execute(insert(s).values(**values).returning(s.c.id))).scalar_one()
    await conn.execute(update(t).values(last_at=func.now()).where(t.c.id == membership.trace_id))
    await notify(conn, membership.system.id)
    if status == "refused" and reason is not None and reason.startswith(system_context.BUDGET_REFUSAL):
        from evo_agents.hub.server import orchestrator  # it opens threads through chat_runs, which joins runs here

        await orchestrator.on_budget(conn, membership, span_id, reason)
    return span_id


async def record_mention(
    conn: AsyncConnection,
    found: Membership | None,
    to_handle: str,
    child: int | None,
    why: str | None,
    message_id: int | None,
) -> None:
    """The mention span of a mention of agent ``to_handle`` by the run of ``found`` that handed run ``child`` (None,
    refused with ``why``): off design when the spec has no interacts edge for it, with no reason; nothing for a run in
    no trace."""
    if found is None or found.trace_id is None:
        return
    off = not system_context.on_design(found.spec, found.node, system_context.node_of(found.spec, to_handle))
    await record_tool(
        conn,
        found,
        system_context.TOOL_MENTION,
        to_handle=to_handle,
        status="ok" if child is not None else "refused",
        off_design=off,
        reason=why,
        message_id=message_id,
        attributes={} if child is None else {"evo.child_run_id": child},
    )


# The budget


async def usage(conn: AsyncConnection, trace_id: int) -> tuple[int, float]:
    """(runs, USD) of trace ``trace_id``: its runs and every run their tree spawned (``spawned_by_run_id``), and what
    they cost (run_spend, the reported cost else the estimate)."""
    s, r, rs = tables.system_spans, tables.runs, tables.run_spend
    seeds = select(s.c.run_id.label("id")).where(
        s.c.trace_id == trace_id, s.c.name == system_context.INVOKE, s.c.run_id.is_not(None)
    )
    tree = seeds.cte("trace_tree", recursive=True)
    tree = tree.union(select(r.c.id).where(r.c.spawned_by_run_id == tree.c.id))
    count = (await conn.execute(select(func.count()).select_from(tree))).scalar_one()
    cost = func.coalesce(func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)), 0)
    usd = (await conn.execute(select(cost).where(rs.c.run_id.in_(select(tree.c.id))))).scalar_one()
    return int(count), float(usd or 0)


async def budget_problem(conn: AsyncConnection, membership: Membership | None) -> str | None:
    """Why the trace of ``membership`` takes no new ask or mention (``system_context.budget_problem``); None while it
    does, or for a run in no trace."""
    if membership is None or membership.trace_id is None:
        return None
    count, usd = await usage(conn, membership.trace_id)
    return system_context.budget_problem(membership.spec, count, usd)


# Briefs


async def save_brief(conn: AsyncConnection, run_id: int, asked_by: int, brief: dict) -> None:
    """Keep the brief an ask by run ``asked_by`` gave run ``run_id``; nothing for an empty one."""
    found = system_context.clean_brief(brief)
    found.pop("reason", None)
    if not found:
        return
    b = tables.agent_ask_briefs
    statement = pg_insert(b).values(run_id=run_id, asked_by_run_id=asked_by, brief=found)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[b.c.run_id], set_={"brief": found, "asked_by_run_id": asked_by})
    )
    if found.get("goal"):
        from evo_agents.hub.server import orchestrator  # it opens threads through chat_runs, which joins runs here

        await orchestrator.check_loop(conn, run_id)


async def brief_of(conn: AsyncConnection, run_id: int) -> tuple[dict, str | None, int | None] | None:
    """(the brief, the asking agent's handle, the asking run) of the session of run ``run_id``: the brief of the run
    an ask made, which the runs that go on in its session keep; None for none."""
    b, r, ap = tables.agent_ask_briefs, tables.runs, tables.agent_profiles
    current = run_id
    for _ in range(MAX_RESUMES):
        row = (await conn.execute(select(b.c.brief, b.c.asked_by_run_id).where(b.c.run_id == current))).one_or_none()
        if row is not None:
            handle = None
            if row.asked_by_run_id is not None:
                query = select(ap.c.handle).join_from(r, ap, ap.c.id == r.c.profile_id)
                handle = (await conn.execute(query.where(r.c.id == row.asked_by_run_id))).scalar_one_or_none()
            return row.brief, handle, row.asked_by_run_id
        current = (await conn.execute(select(r.c.resume_of_run_id).where(r.c.id == current))).scalar_one_or_none()
        if current is None:
            return None
    return None


# The default knowledge graph


async def prompt_parts(conn: AsyncConnection, run_id: int) -> tuple[list[str], list[str]]:
    """(the System section, the brief block) of the prompt of chat run ``run_id``: its place in the agent system it
    runs in (``system_context.system_lines``), and the brief of the ask that made its session
    (``system_context.brief_lines``); [] for each it lacks."""
    found = await of_run(conn, run_id)
    place = None if found is None else system_context.place_of(found.system.slug, found.spec, found.handle)
    briefed = await brief_of(conn, run_id)
    brief = [] if briefed is None else system_context.brief_lines(*briefed)
    return ([] if place is None else system_context.system_lines(place)), brief


async def default_graph(conn: AsyncConnection, run_id: int, project_id: int) -> tuple[str, frozenset[str]] | None:
    """(a name, the sources) the kg tools of run ``run_id`` read when a call names no graph: the union of the named
    graphs its agent uses in its system (uses_kg), named after the graph when it is one, so that its spans count for
    its kg node, and ``system:<slug>`` for several; None for a run of no system, of another project, with no such edge,
    or when one of them is ``default`` (every source)."""
    from evo_agents.hub import kg_named_graphs

    found = await of_run(conn, run_id)
    if found is None or found.system.project_id != project_id:
        return None
    graphs = system_context.kg_graphs(found.spec, found.handle)
    if not graphs or "default" in graphs:
        return None
    sources: set[str] = set()
    for graph in graphs:
        sources |= await kg_named_graphs.sources_of(conn, project_id, graph) or frozenset()
    return (graphs[0] if len(graphs) == 1 else f"system:{found.system.slug}"), frozenset(sources)


def results_read(result) -> int | None:
    """How many results a kg tool's ``result`` (an MCP result) says it read, from its text's READ_META; None when it
    says none, as an error or kg_status."""
    for block in (result.get("content") or []) if isinstance(result, dict) else []:
        meta = block.get("_meta") if isinstance(block, dict) else None
        read = meta.get(READ_META) if isinstance(meta, dict) else None
        count = read.get("results_read") if isinstance(read, dict) else None
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


async def record_kg(conn: AsyncConnection, run_id: int, tool: str, graph: str | None, result=None) -> None:
    """An execute_tool span of a kg tool call of run ``run_id`` in a trace, the graph it read as its data source and,
    when ``result`` says it, how many results it read (``evo.kg.results``: 0 is a query that came back empty, which
    the Curator's lens agent_system counts)."""
    found = await of_run(conn, run_id)
    if found is None or found.trace_id is None:
        return
    attributes: dict = {"gen_ai.data_source.id": graph or "default"}
    if (count := results_read(result)) is not None:
        attributes[KG_RESULTS] = count
    await record_tool(conn, found, tool, attributes=attributes)


# Reading


async def run_numbers(conn: AsyncConnection, run_ids: list[int]) -> dict[int, dict]:
    """{run: {input_tokens, output_tokens, usd, state, seconds}} of chat runs ``run_ids``, from run_spend and runs."""
    if not run_ids:
        return {}
    r, rs = tables.runs, tables.run_spend
    spend = (
        select(
            rs.c.run_id,
            func.sum(rs.c.input_tokens).label("input_tokens"),
            func.sum(rs.c.output_tokens).label("output_tokens"),
            func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)).label("usd"),
        )
        .where(rs.c.run_id.in_(run_ids))
        .group_by(rs.c.run_id)
    )
    found = {row.run_id: dict(row._mapping) for row in await conn.execute(spend)}
    states = select(r.c.id, r.c.state, r.c.model).where(r.c.id.in_(run_ids))
    out = {}
    for row in await conn.execute(states):
        numbers = found.get(row.id, {})
        out[row.id] = {
            "state": row.state,
            "model": row.model,
            "input_tokens": int(numbers.get("input_tokens") or 0),
            "output_tokens": int(numbers.get("output_tokens") or 0),
            "usd": float(numbers.get("usd") or 0),
        }
    return out
