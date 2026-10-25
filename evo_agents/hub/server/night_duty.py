"""Where the night shift's runs go (plan worker-pools, criterion a4): the worker its charter names, or the pool.

A charter names ``worker`` or ``pool``, exactly one. The worker on duty is one of the writer's own live workers that
serves the project and takes runs dispatched by the hub, as before; the pool is one of the writer's own pools that
lists the project (``duty_target``). The schedule follows it: its owner is the worker's, or the pool's, owner.

Each pass of the night shift reads the duty now (``on_duty``): a worker as a dispatch pinned to it sees it, or, for a
pool, its nodes that are ready to take a run now (live, online, serving the project, not draining, taking runs
dispatched by the hub, ready by their doctor report, and with no taint of their own beyond the pool's). A pool with no
such node waits, as a worker offline does, and says so. A run of a pool's schedule goes there by the selector
pool=<name>, tolerating the pool's taints, instead of being pinned: any ready node of the pool that fits it claims it
(``Duty.columns``); a run of a worker's schedule stays pinned to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.runs.models import available
from evo_agents.hub.server.runs.models.placement import selector_of
from evo_agents.hub.server.runs.service.dispatch import Pinned, unfit
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.worker_placement import Placement, columns

if TYPE_CHECKING:
    from evo_agents.hub.server.projects import ProjectAccess


@dataclass(frozen=True)
class DutyTarget:
    """What a charter names: a worker (``worker_id``) or a pool (``pool_id``), its name, and its owner."""

    worker_id: int | None
    pool_id: int | None
    name: str
    owner_id: int

    @property
    def key(self) -> str:
        return "worker" if self.pool_id is None else "pool"

    @property
    def where(self) -> str:
        return f"{self.key}={self.name}"


def _fresh(w):
    return func.coalesce(w.c.last_heartbeat_at > func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS), False)


def _serves(w, project_id):
    wp = tables.worker_projects
    return exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id)


async def _duty_worker(conn: AsyncConnection, user: Principal, access: ProjectAccess, name: str, current):
    w = tables.workers
    if current is not None and current.worker_id is not None and current.worker_name.lower() == name.lower():
        row = (await conn.execute(select(w.c.owner_id).where(w.c.id == current.worker_id))).one()
        return DutyTarget(current.worker_id, None, current.worker_name, row.owner_id)
    query = select(w.c.id, w.c.name, w.c.dispatch_from, _serves(w, access.project_id).label("serves")).where(
        w.c.owner_id == user.user_id, func.lower(w.c.name) == name.lower(), w.c.revoked_at.is_(None)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(
            403,
            f"the night shift runs on a worker of the admin who names it, and you have no live worker {name}: "
            "register the machine, or keep the charter's worker; nothing was written",
        )
    if not row.serves:
        raise HTTPException(409, f"worker {row.name} does not take runs of project {access.name}: register it for it")
    if row.dispatch_from == "web":
        raise HTTPException(
            409,
            f"worker {row.name} takes only runs dispatched from a web session, as its owner set it, so the night shift "
            "could never hand it a run: let it take runs from anywhere, or name another worker; nothing was written",
        )
    return DutyTarget(row.id, None, row.name, user.user_id)


async def _duty_pool(conn: AsyncConnection, user: Principal, access: ProjectAccess, name: str, current):
    p, pp = tables.worker_pools, tables.worker_pool_projects
    listed = exists().where(pp.c.pool_id == p.c.id, pp.c.project_id == access.project_id)
    mine = p.c.owner_id == user.user_id
    if current is not None and current.pool_id is not None:
        mine = or_(mine, p.c.id == current.pool_id)
    query = select(p.c.id, p.c.name, p.c.owner_id, listed.label("listed")).where(
        mine, func.lower(p.c.name) == name.lower()
    )
    row = (await conn.execute(query.order_by((p.c.owner_id != user.user_id), p.c.id).limit(1))).one_or_none()
    if row is None:
        raise HTTPException(
            403,
            f"the night shift runs on a pool of the admin who names it, and you have no pool {name}: create it "
            "(POST /v1/worker-pools), or keep the charter's pool; nothing was written",
        )
    if not row.listed:
        raise HTTPException(
            409,
            f"pool {row.name} does not serve project {access.name}: add the project to the pool "
            "(PATCH /v1/worker-pools/{name}); nothing was written",
        )
    return DutyTarget(None, row.id, row.name, row.owner_id)


async def duty_target(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, worker: str | None, pool: str | None, current
) -> DutyTarget:
    """The worker or pool a charter names: the current one when it names it again, else one of ``user``'s own; 403 for
    none such, 409 for one the night shift could never hand a run."""
    if pool is not None:
        return await _duty_pool(conn, user, access, pool, current)
    return await _duty_worker(conn, user, access, worker, current)


# The duty now


@dataclass(frozen=True)
class Duty:
    """Where the night shift's runs go now: the worker on duty (``nodes`` its one entry), or the ready nodes of
    ``pool``, its runs tolerating ``tolerations``, the pool's taints."""

    nodes: tuple[Pinned, ...]
    pool: str | None = None
    tolerations: tuple[str, ...] = ()

    @property
    def where(self) -> str:
        """``worker <name>`` or ``pool <name>``, for the text of a run's first event and the logs."""
        return f"pool {self.pool}" if self.pool is not None else f"worker {self.nodes[0].name}"

    @property
    def name(self) -> str:
        return self.pool if self.pool is not None else self.nodes[0].name

    def unfit(self, project: str, kind: str, repos: list[str], runtime: str, **wanted: bool) -> list[str]:
        """What keeps every node from ever claiming the run (``dispatch.unfit``); empty when one of them would."""
        found = []
        for node in self.nodes:
            problems = unfit(node, project, kind, repos, runtime, **wanted)
            if not problems:
                return []
            found.append((node, problems))
        node, problems = found[0]
        if self.pool is None:
            return problems
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
        if self.pool is None:
            return {"pinned_worker_id": self.nodes[0].id}
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


async def _worker_duty(conn: AsyncConnection, worker_id: int, project_id: int) -> tuple[Duty | None, str | None]:
    row = (await conn.execute(_worker_query(project_id).where(tables.workers.c.id == worker_id))).one()
    why = _why_not(row)
    return (None, f"worker {row.name} is {why}") if why else (Duty((_pinned(worker_id, row),)), None)


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


async def on_duty(conn: AsyncConnection, due) -> tuple[Duty | None, str | None]:
    """Where schedule ``due``'s runs go now (``Duty``), or why nothing of it takes a run now."""
    if due.pool_id is not None:
        return await _pool_duty(conn, due.pool_id, due.project_id)
    return await _worker_duty(conn, due.worker_id, due.project_id)
