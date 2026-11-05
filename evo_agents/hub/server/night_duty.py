"""Where the night shift's runs go: the pool of the project's default agent.

Each pass of the night shift reads the duty now (``on_duty``): the nodes of the default agent's pool that are ready to
take a run now (live, online, serving the project, not draining, taking runs dispatched by the hub, ready by their
doctor report, and with no taint of their own beyond the pool's). A pool with no such node waits, and says so. A run
of the night shift goes there by the selector pool=<name>, tolerating the pool's taints: any ready node of the pool
that fits it claims it (``Duty.columns``).

A KG run of a project fed by uploads alone (``kg_duty``) goes to the same pool when it has a ready node, else to the
shared KG pool when that may take it (``kg_shared_pool.for_project``), which then serves the project.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import agent_placement, kg_shared_pool
from evo_agents.hub.server.runs.models import available
from evo_agents.hub.server.runs.models.placement import selector_of
from evo_agents.hub.server.runs.service.dispatch import Pinned, unfit
from evo_agents.hub.server.worker_placement import Placement, columns


def _fresh(w):
    return func.coalesce(w.c.last_heartbeat_at > func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS), False)


def _serves(w, project_id):
    wp = tables.worker_projects
    return exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id)


# The duty now


@dataclass(frozen=True)
class Duty:
    """Where the night shift's runs go now: the ready nodes of ``pool``, its runs tolerating ``tolerations``, the
    pool's taints."""

    nodes: tuple[Pinned, ...]
    pool: str
    tolerations: tuple[str, ...] = ()
    shared: bool = False  # the shared KG pool's, for a KG run (``kg_duty``)

    @property
    def where(self) -> str:
        """``pool <name>``, for the text of a run's first event and the logs."""
        return f"pool {self.pool}"

    @property
    def name(self) -> str:
        return self.pool

    def unfit(self, project: str, kind: str, repos: list[str], runtime: str, **wanted: bool) -> list[str]:
        """What keeps every node from ever claiming the run (``dispatch.unfit``); empty when one of them would."""
        found = []
        for node in self.nodes:
            problems = unfit(node, project, kind, repos, runtime, **wanted)
            if not problems:
                return []
            found.append((node, problems))
        node, problems = found[0]
        return [f"no ready node of pool {self.pool} fits it: {node.name}: {'; '.join(problems)}"]

    def runtime(self, name: str) -> dict | None:
        """What the first node reporting runtime ``name`` available says of it; None when none does."""
        for node in self.nodes:
            report = node.runtimes.get(name)
            if available(report):
                return report
        return None

    def columns(self) -> dict:
        """The columns of tables.runs that place a run of the night shift here."""
        return {"pinned_worker_id": None, "selector": selector_of(self.pool, ()), "tolerations": list(self.tolerations)}


def _pinned(worker_id: int, row) -> Pinned:
    return Pinned(
        worker_id,
        row.name,
        row.runtimes or {},
        row.checkouts or {},
        row.agent_version,
        tuple(row.run_kinds or ()),
        tuple(row.features or ()),
        tuple(row.labels or ()),
        tuple(row.plugins or ()),
        row.place_pool,
    )


def _worker_query(project_id: int):
    w = tables.workers
    return select(
        w.c.id,
        w.c.name,
        w.c.runtimes,
        w.c.checkouts,
        w.c.agent_version,
        w.c.dispatch_from,
        w.c.revoked_at,
        w.c.drained_at,
        w.c.run_kinds,
        w.c.features,
        w.c.labels,
        w.c.plugins,
        _fresh(w).label("fresh"),
        _serves(w, project_id).label("serves"),
        *columns(w),
    )


def _why_not(row) -> str | None:
    if row.revoked_at is not None:
        return "revoked"
    if not row.serves:
        return "not serving the project"
    if row.dispatch_from == "web":
        return "taking runs dispatched from a web session only"
    if row.drained_at is not None:
        return "draining"
    if not row.fresh:
        return "offline"
    return None


async def _pool_duty(conn: AsyncConnection, pool_id: int, project_id: int) -> tuple[Duty | None, str | None]:
    p, w = tables.worker_pools, tables.workers
    pool = (await conn.execute(select(p.c.name, p.c.taints).where(p.c.id == pool_id))).one()
    query = _worker_query(project_id).where(w.c.pool_id == pool_id, w.c.revoked_at.is_(None)).order_by(w.c.id)
    rows = (await conn.execute(query)).all()
    tolerations = tuple(dict.fromkeys(taint.lower() for taint in pool.taints or ()))
    nodes, why = [], []
    for row in rows:
        placed = Placement.of(row)
        reason = _why_not(row) or (f"not ready: {placed.unready}" if not placed.ready else None)
        if reason is None and placed.untolerated(tolerations):
            reason = f"tainted {', '.join(placed.untolerated(tolerations))} beyond the pool's taints"
        if reason is None:
            nodes.append(_pinned(row.id, row))
        else:
            why.append(f"{row.name} is {reason}")
    if nodes:
        return Duty(tuple(nodes), pool.name, tolerations), None
    if not rows:
        return None, f"pool {pool.name} has no live node: add one with a join token"
    return None, f"pool {pool.name} has no node ready for a run now ({'; '.join(why)})"


NO_POOL = "the default agent of the project has no pool: create the agent's pool and add a node to it"


async def on_duty(conn: AsyncConnection, due) -> tuple[Duty | None, str | None]:
    """Where schedule ``due``'s runs go now (``Duty``), or why nothing of it takes a run now."""
    home = await agent_placement.default_home(conn, due.project_id)
    if home is None:
        return None, NO_POOL
    duty, why = await _pool_duty(conn, home.pool_id, due.project_id)
    if duty is None:
        await agent_placement.watch(conn, home)
    return duty, why


async def kg_duty(conn: AsyncConnection, due) -> tuple[Duty | None, str | None]:
    """Where a KG run of ``due``'s project goes now (the review runs with lens kg_questions and kg_init): the ready
    nodes of its default agent's pool, else those of the shared KG pool when it may take the project's KG runs, which
    then serves the project. With neither, why; the admins hear the agent's pool is empty only then."""
    home = await agent_placement.default_home(conn, due.project_id)
    duty, why = (None, NO_POOL) if home is None else await _pool_duty(conn, home.pool_id, due.project_id)
    if duty is not None:
        return duty, None
    shared = await kg_shared_pool.for_project(conn, due.project_id)
    if shared is not None:
        await kg_shared_pool.serve(conn, shared, due.project_id)
        found, not_shared = await _pool_duty(conn, shared.pool_id, due.project_id)
        if found is not None:
            return replace(found, shared=True), None
        why = f"{why}; the shared KG pool: {not_shared}"
    await pool_empty(conn, due.project_id)
    return None, why


async def pool_empty(conn: AsyncConnection, project_id: int) -> None:
    """Tell the admins of the project once that its default agent's pool has no ready node (``agent_placement.watch``)."""
    home = await agent_placement.default_home(conn, project_id)
    if home is not None:
        await agent_placement.watch(conn, home)
