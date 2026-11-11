"""Two runs that wait on each other: the wait graph of agent asks, the Orchestrator's check before a reassign, and the
reaper's pass that breaks a cycle (``docs/hub.md``, Agents across projects).

An open wait of an ask (``decisions.awaits_run_id``) is an edge of the graph: from the run that holds it (a run's open
decisions move to the run that resumes it, so this is the last run of its session) to the last run of the chain of
resumed runs (``runs.resume_of_run_id``) of the run it awaits, which is the run whose end answers it
(``agent_asks._waits``). When the edges close a cycle, no run of it ends, and each waits until its wait's time, days
later (``run_waits.MAX_WAIT``). On 2026-11-11 runs #4982 of @ea-lead and #4984 of @ea-dev did: #4984 went on from
#4574, whose escalation wait #1119 the Orchestrator (#4980, going on from #4575) gave with the work to @ea-lead, whose
session (#4573, going on as #4982) already waited on #4574 (wait #1118) and asked it again (wait #1143).

- ``upstream``: the agents whose runs wait, directly or through other runs, on a run's chain. The Orchestrator's
  reassign refuses them while the thread's escalation wait is open (``triage_rules.reassign_problem``): moving the
  wait to their run would close a cycle.
- ``break_cycles``, on each pass of the reaper (``run_reaper.recover_runs``): each cycle of the graph is broken once,
  at the run below, the one whose hop is the deepest (``to_answer``): its wait is answered with the last message of
  the run it awaits (``agent_asks.last_reply``), which wakes it, and both runs get an event saying why.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables

log = logging.getLogger(__name__)

MAX_CHAIN = 200  # runs of one chain of resumed runs the graph follows, a bound on a corrupt chain


@dataclass(frozen=True)
class Edge:
    """Open wait ``wait_id`` of run ``waiter``, which the end of run ``awaited`` (the last of its chain) answers."""

    wait_id: int
    waiter: int
    awaited: int


@dataclass(frozen=True)
class Node:
    """What breaking a cycle reads of a run of the graph: its hop and its agent's handle."""

    hop: int
    handle: str | None


def last_runs(resumed: dict[int, int], ids: Iterable[int]) -> dict[int, int]:
    """The last run of the chain of resumed runs of each run of ``ids``; ``resumed`` maps a run to the run that goes
    on from it."""
    found = {}
    for run_id in ids:
        last, seen = run_id, 0
        while last in resumed and seen < MAX_CHAIN:
            last, seen = resumed[last], seen + 1
        found[run_id] = last
    return found


def edges_of(waits: Iterable[tuple[int, int, int]], resumed: dict[int, int]) -> list[Edge]:
    """The edges of open waits ``waits``, each (id, run holding it, run it awaits)."""
    waits = list(waits)
    last = last_runs(resumed, {awaited for _, _, awaited in waits})
    return [Edge(wait_id, waiter, last[awaited]) for wait_id, waiter, awaited in waits]


def cycles(edges: list[Edge]) -> list[list[Edge]]:
    """The cycles of ``edges``, each the edges of one, at most one a set of runs, in the order of the first run."""
    out: dict[int, list[Edge]] = {}
    for edge in sorted(edges, key=lambda e: e.wait_id):
        out.setdefault(edge.waiter, []).append(edge)
    found: dict[frozenset[int], list[Edge]] = {}
    done: set[int] = set()

    def walk(run_id: int, path: list[Edge], on_path: dict[int, int]) -> None:
        for edge in out.get(run_id, []):
            if edge.awaited in on_path:
                cycle = path[on_path[edge.awaited] :] + [edge]
                found.setdefault(frozenset(e.waiter for e in cycle), cycle)
            elif edge.awaited not in done:
                on_path[edge.awaited] = len(path) + 1
                walk(edge.awaited, [*path, edge], on_path)
                del on_path[edge.awaited]
        done.add(run_id)

    for start in sorted(out):
        if start not in done:
            walk(start, [], {start: 0})
    return list(found.values())


def to_answer(cycle: list[Edge], nodes: dict[int, Node]) -> Edge:
    """The edge of ``cycle`` the hub breaks: the wait of the run below, the deepest hop of the cycle (the run the
    others' work reached last), the newest run on a tie."""
    return max(cycle, key=lambda e: (nodes[e.waiter].hop if e.waiter in nodes else 0, e.waiter, e.wait_id))


def upstream(edges: list[Edge], start: int) -> set[int]:
    """The runs that wait, directly or through other runs, on run ``start`` (the last of its chain)."""
    into: dict[int, list[int]] = {}
    for edge in edges:
        into.setdefault(edge.awaited, []).append(edge.waiter)
    seen: set[int] = set()
    todo = [start]
    while todo:
        for waiter in into.get(todo.pop(), []):
            if waiter not in seen and waiter != start:
                seen.add(waiter)
                todo.append(waiter)
    return seen


# On the hub


async def _resumed(conn: AsyncConnection, ids: set[int]) -> dict[int, int]:
    """The run that goes on from each run of the chains of resumed runs that start at ``ids``."""
    r = tables.runs
    resumed: dict[int, int] = {}
    todo = set(ids)
    for _ in range(MAX_CHAIN):
        if not todo:
            break
        query = select(r.c.resume_of_run_id, r.c.id).where(r.c.resume_of_run_id.in_(todo)).order_by(r.c.id)
        rows = (await conn.execute(query)).all()
        todo = set()
        for earlier, later in rows:
            if earlier not in resumed:
                resumed[earlier] = later
                todo.add(later)
    return resumed


async def graph(conn: AsyncConnection, also: Iterable[int] = ()) -> tuple[list[Edge], dict[int, int]]:
    """The edges of every open wait of an ask, and the last run of the chain of each run of ``also``."""
    d = tables.decisions
    query = select(d.c.id, d.c.run_id, d.c.awaits_run_id).where(
        d.c.state == "open", d.c.category == runs.WAIT_DECISION_CATEGORY, d.c.awaits_run_id.is_not(None)
    )
    waits = [tuple(row) for row in (await conn.execute(query.order_by(d.c.id))).all()]
    extra = set(also)
    resumed = await _resumed(conn, {row[2] for row in waits} | extra)
    return edges_of(waits, resumed), last_runs(resumed, extra)


async def _nodes(conn: AsyncConnection, ids: Iterable[int]) -> dict[int, Node]:
    r, p = tables.runs, tables.agent_profiles
    query = select(r.c.id, r.c.hop, p.c.handle).outerjoin_from(r, p, p.c.id == r.c.profile_id).where(r.c.id.in_(ids))
    return {row.id: Node(row.hop or 0, row.handle) for row in (await conn.execute(query)).all()}


async def upstream_handles(conn: AsyncConnection, run_id: int) -> set[str]:
    """The handles of the agents whose runs wait, directly or through other runs, on the chain of run ``run_id``."""
    edges, last = await graph(conn, [run_id])
    waiting = upstream(edges, last[run_id])
    return {node.handle for node in (await _nodes(conn, waiting)).values() if node.handle}


def _why(edge: Edge, cycle: list[Edge], nodes: dict[int, Node]) -> str:
    named = ", ".join(f"#{e.waiter} (@{nodes[e.waiter].handle if e.waiter in nodes else '?'})" for e in cycle)
    return (
        f"runs {named} wait on each other in a cycle: none of them ends, so none of their waits is answered. The hub "
        f"broke the cycle at run #{edge.waiter}, the one below: its wait #{edge.wait_id} is answered with the last "
        f"message of run #{edge.awaited}."
    )


async def _break(conn: AsyncConnection, edge: Edge) -> int | None:
    """Answer wait ``edge.wait_id`` when it still closes a cycle, in the caller's transaction; the run it woke."""
    from evo_agents.hub.server import agent_asks, run_waits  # they hand work through the routes that import this
    from evo_agents.hub.server.run_state import write_event

    edges, _ = await graph(conn)
    now = next((e for e in edges if e.wait_id == edge.wait_id), None)
    cycle = next((c for c in cycles(edges) if now in c), None)
    if now is None or cycle is None:  # answered, moved or no longer a cycle since the read
        return None
    nodes = await _nodes(conn, {e.waiter for e in cycle} | {now.awaited})
    why = _why(now, cycle, nodes)
    reply = await agent_asks.last_reply(conn, now.awaited)
    said = reply[1].strip() if reply else "(it wrote no message in its thread)"
    handle = nodes[now.awaited].handle if now.awaited in nodes else "?"
    text = (
        f"Decision #{now.wait_id} (wait) woke: {why}\n\nThe last message of @{handle} (run #{now.awaited}):\n\n{said}"
    )
    d = tables.decisions
    row = (await conn.execute(select(d.c.project_id, d.c.plan_id).where(d.c.id == now.wait_id))).one()
    woke = await run_waits.wake(conn, now.wait_id, row.project_id, row.plan_id, runs.clip(text, runs.MAX_ANSWER_BYTES))
    if woke is None:
        return None
    event = {
        "text": f"Wait cycle broken: {why}",
        "wait_cycle": {"wait": now.wait_id, "runs": [e.waiter for e in cycle]},
    }
    r = tables.runs
    await conn.execute(select(r.c.id).where(r.c.id == now.awaited).with_for_update())  # write_event's lock
    for run_id in dict.fromkeys([woke, now.awaited]):
        await write_event(conn, run_id, event)
    return woke


async def break_cycles(engine: AsyncEngine) -> int:
    """Break each cycle of the wait graph once, each in a transaction of its own; how many broke."""
    async with engine.begin() as conn:
        edges, _ = await graph(conn)
        found = cycles(edges)
        if not found:
            return 0
        nodes = await _nodes(conn, {e.waiter for cycle in found for e in cycle})
    broken = 0
    for cycle in found:
        edge = to_answer(cycle, nodes)
        async with engine.begin() as conn:
            woke = await _break(conn, edge)
        if woke is not None:
            broken += 1
            log.info("wait cycle broken", extra={"decision_id": edge.wait_id, "run_id": woke})
    return broken
