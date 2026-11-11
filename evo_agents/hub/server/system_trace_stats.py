"""The numbers of each trace of an agent system, read from the database for its spread (``docs/hub.md``, Agent systems:
the spread of traces; ``system_context.trace_row`` and ``distribution``). Read only: nothing here changes the budget,
the hops, the loop rule or off_design.

``numbers`` reads, for a batch of traces at once: the runs of each trace and of the tree they spawned
(``spawned_by_run_id``), their cost and deepest hop, as ``system_traces.usage`` counts them for the budget; the asks
and mentions the budget refused (``refused`` execute_tool spans whose reason starts with
``system_context.BUDGET_REFUSAL``; an ask refused by the budget whose agent gave ``--reason`` keeps that reason
instead, so it is not counted); the Orchestrator threads opened on the trace by rule; and the attempts to cross
each edge: the turns handed over it (``invoke_agent`` spans that are no resume) and the asks and mentions refused on
it, each off design or not. A refused span is off design when the spec of its revision has no edge for it, since a
refusal by the budget stores off_design false.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context, tables
from evo_agents.hub.server import system_store, system_traces


async def tree_usage(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, tuple[int, float, int | None]]:
    """{trace: (runs, USD, deepest hop)}, the tree and cost of ``system_traces.usage`` for each trace."""
    s, r, rs = tables.system_spans, tables.runs, tables.run_spend
    seeds = select(s.c.trace_id, s.c.run_id.label("id")).where(
        s.c.trace_id.in_(trace_ids), s.c.name == system_context.INVOKE, s.c.run_id.is_not(None)
    )
    tree = seeds.cte("trace_trees", recursive=True)
    tree = tree.union(select(tree.c.trace_id, r.c.id).where(r.c.spawned_by_run_id == tree.c.id))
    counted = (
        select(tree.c.trace_id, func.count().label("runs"), func.max(r.c.hop).label("hop"))
        .select_from(tree.outerjoin(r, r.c.id == tree.c.id))
        .group_by(tree.c.trace_id)
    )
    found = {row.trace_id: [int(row.runs), 0.0, row.hop] for row in await conn.execute(counted)}
    cost = func.coalesce(func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)), 0)
    spent = select(tree.c.trace_id, cost.label("usd")).join(rs, rs.c.run_id == tree.c.id).group_by(tree.c.trace_id)
    for row in await conn.execute(spent):
        found[row.trace_id][1] = float(row.usd or 0)
    return {trace_id: (runs, usd, hop) for trace_id, (runs, usd, hop) in found.items()}


async def _refusals(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, int]:
    s = tables.system_spans
    query = (
        select(s.c.trace_id, func.count())
        .where(
            s.c.trace_id.in_(trace_ids),
            s.c.name == system_context.TOOL,
            s.c.status == "refused",
            s.c.reason.startswith(system_context.BUDGET_REFUSAL, autoescape=True),
        )
        .group_by(s.c.trace_id)
    )
    return {trace_id: int(count) for trace_id, count in await conn.execute(query)}


async def _threads(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, dict[str, int]]:
    t = tables.orchestrator_threads
    query = (
        select(t.c.trace_id, t.c.rule, func.count())
        .where(t.c.trace_id.in_(trace_ids), t.c.rule.in_(system_context.THREAD_RULES))
        .group_by(t.c.trace_id, t.c.rule)
    )
    found: dict[int, dict[str, int]] = {}
    for trace_id, rule, count in await conn.execute(query):
        found.setdefault(trace_id, {})[rule] = int(count)
    return found


async def _crossings(
    conn: AsyncConnection, trace_ids: list[int]
) -> dict[int, list[tuple[str | None, str | None, bool, bool]]]:
    """{trace: [(from, to, off design as stored, refused)]} of the attempts to cross an edge."""
    s = tables.system_spans
    handed = (
        (s.c.name == system_context.INVOKE) & s.c.from_node.is_not(None) & ~s.c.attributes.has_key(system_traces.RESUME)
    )
    refused = (
        (s.c.name == system_context.TOOL)
        & s.c.tool.in_((system_context.TOOL_ASK, system_context.TOOL_MENTION))
        & (s.c.status == "refused")
    )
    query = (
        select(s.c.trace_id, s.c.name, s.c.from_node, s.c.node, s.c.to_node, s.c.off_design)
        .where(s.c.trace_id.in_(trace_ids), handed | refused)
        .order_by(s.c.trace_id, s.c.id)
    )
    found: dict[int, list[tuple[str | None, str | None, bool, bool]]] = {}
    for row in await conn.execute(query):
        handed_over = row.name == system_context.INVOKE
        end = row.node if handed_over else row.to_node
        found.setdefault(row.trace_id, []).append((row.from_node, end, bool(row.off_design), not handed_over))
    return found


async def numbers(conn: AsyncConnection, traces: list[tuple[int, int | None, int]]) -> dict[int, dict]:
    """{trace: ``system_context.trace_row``} of ``traces``, each (id, system id, revision): the limits are those of
    the revision the trace ran under, the defaults once its system is deleted."""
    trace_ids = [trace_id for trace_id, _, _ in traces]
    if not trace_ids:
        return {}
    used = await tree_usage(conn, trace_ids)
    refusals = await _refusals(conn, trace_ids)
    threads = await _threads(conn, trace_ids)
    crossings = await _crossings(conn, trace_ids)
    specs: dict[tuple[int, int], dict] = {}
    out = {}
    for trace_id, system_id, revision in traces:
        spec: dict = {}
        if system_id is not None:
            if (system_id, revision) not in specs:
                specs[(system_id, revision)] = await system_store.revision_spec(conn, system_id, revision) or {}
            spec = specs[(system_id, revision)]
        runs, usd, hop = used.get(trace_id, (0, 0.0, None))
        out[trace_id] = system_context.trace_row(
            spec,
            usd=usd,
            runs=runs,
            max_hop=hop,
            budget_refusals=refusals.get(trace_id, 0),
            threads=threads.get(trace_id, {}),
            crossings=[
                # a refusal by the budget stores off_design false whatever the edge: the spec says
                (start, end, off or (refused and not system_context.on_design(spec, start, end)))
                for start, end, off, refused in crossings.get(trace_id, [])
            ],
        )
    return out
