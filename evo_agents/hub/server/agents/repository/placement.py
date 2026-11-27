"""The SQL of placement by agent: the pool of an agent, the nodes of a pool, the notice of an empty pool, and the
conditions of the claim on the run being claimed (``pooled_run``, ``home_run``, ``claim_gate``)."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import kg_shared_pool
from evo_agents.hub.server.agents.models.placement import Home
from evo_agents.hub.server.workers.repository import placement as worker_placement


async def home_of(conn: AsyncConnection, profile_id: int) -> Home | None:
    """The pool of agent ``profile_id``; None for a profile with no pool."""
    ap, wp = tables.agent_profiles, tables.worker_pools
    query = (
        select(wp.c.id, wp.c.name, wp.c.taints, ap.c.id.label("profile_id"), ap.c.handle, ap.c.project_id)
        .join_from(ap, wp, wp.c.agent_id == ap.c.id)
        .where(ap.c.id == profile_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return Home(row.id, row.name, tuple(row.taints or ()), row.profile_id, row.handle, row.project_id)


async def pinned_inside(conn: AsyncConnection, home: Home, worker_id: int) -> bool:
    """Whether worker ``worker_id`` is a node of the pool of ``home``."""
    w = tables.workers
    return bool(await conn.scalar(select(w.c.pool_id == home.pool_id).where(w.c.id == worker_id)))


def _node_columns():
    from evo_agents.hub.server.runs.service.worker_fit import OFFLINE  # worker_fit imports worker_placement

    w = tables.workers
    return [
        w.c.id,
        w.c.name,
        (w.c.last_heartbeat_at >= func.now() - OFFLINE).label("online"),
        w.c.drained_at,
        *worker_placement.columns(w),
    ]


async def nodes(conn: AsyncConnection, home: Home) -> list:
    """The rows (id, name, online, drained_at and the placement columns) of the live nodes of the pool, the oldest
    first."""
    w = tables.workers
    query = select(*_node_columns()).where(w.c.pool_id == home.pool_id, w.c.revoked_at.is_(None))
    return list((await conn.execute(query.order_by(w.c.id))).all())


async def mark_empty(conn: AsyncConnection, pool_id: int) -> bool:
    """Mark the pool as one the admins were told is empty; False when it was marked already."""
    wp = tables.worker_pools
    taken = await conn.execute(
        update(wp)
        .values(empty_notified_at=func.now())
        .where(wp.c.id == pool_id, wp.c.empty_notified_at.is_(None))
        .returning(wp.c.id)
    )
    return taken.scalar_one_or_none() is not None


async def recovered(conn: AsyncConnection, pool_id: int) -> None:
    """The pool has a ready node: the next time it has none, the admins hear of it again."""
    wp = tables.worker_pools
    await conn.execute(
        update(wp).values(empty_notified_at=None).where(wp.c.id == pool_id, wp.c.empty_notified_at.is_not(None))
    )


async def recovered_by(conn: AsyncConnection, worker_id: int) -> None:
    """``recovered`` for the pool of the ready node ``worker_id``, which has just polled for a run."""
    w, wp = tables.workers, tables.worker_pools
    of_worker = select(w.c.pool_id).where(w.c.id == worker_id).scalar_subquery()
    await conn.execute(
        update(wp).values(empty_notified_at=None).where(wp.c.id == of_worker, wp.c.empty_notified_at.is_not(None))
    )


async def admins(conn: AsyncConnection, project_id: int) -> list[int]:
    """The ids of the admins of the project."""
    g = tables.grants
    found = await conn.execute(select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin"))
    return list(found.scalars())


# The claim


# Aliases, since the claim reads both tables; built once, as an alias of the wide workers table cost each claim a third
# of its time.
_NODE, _HOME = tables.workers.alias("node"), tables.worker_pools.alias("home")


def _agent_of(worker_id: int):
    """The agent whose pool worker ``worker_id`` is a node of; NULL for any other worker (a scalar subquery)."""
    w, wp = _NODE, _HOME
    return select(wp.c.agent_id).join_from(w, wp, wp.c.id == w.c.pool_id).where(w.c.id == worker_id).scalar_subquery()


def pooled_run():
    """Whether the run being claimed (``tables.runs``, correlated) is of an agent: every run but a ``local`` one."""
    return tables.runs.c.profile_id.is_not(None)


def home_run(worker_id: int):
    """Whether the run being claimed is of an agent whose pool worker ``worker_id`` is a node of."""
    return and_(pooled_run(), _agent_of(worker_id) == tables.runs.c.profile_id)


def claim_gate(worker_id: int):
    """The condition on the run being claimed that placement by agent holds: a run of an agent goes to a node of that
    agent's pool, or to a node of the shared KG pool that may take it (``kg_shared_pool.takes``), and a node of an
    agent's pool takes only the runs of that agent."""
    r = tables.runs
    home = _agent_of(worker_id)
    return and_(
        or_(~pooled_run(), home == r.c.profile_id, kg_shared_pool.takes(worker_id)),
        or_(home.is_(None), r.c.profile_id == home),
    )
