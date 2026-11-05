"""The shared KG pool (plan kg-zip-to-answers, criterion a1): one pool of a member that a hub admin names
(``worker_pools.kg_shared``, schema 0205; ``evo_agents.hub.server.kg_shared_pool_api``), whose ready nodes take the KG
runs of a project fed by uploads alone when the pool of its default agent has none.

A KG run is a run of a kind of ``KINDS`` with no repo: today the review runs with lens kg_questions and kg_init
(``evo_agents.hub.server.kg_init``). The pool takes one only when the project has no repo and its creator owns the pool;
the project of any other member never goes there, and every other run keeps the rule of the agent pools
(``agent_placement``).

- ``for_project`` is the shared pool a project's KG runs may go to, None when they may not.
- ``serve`` adds the project to the pool and to its live nodes, so a new project needs no person to do it.
- ``placed`` keeps a KG run on the shared pool when ``agent_placement.stamp`` places a run; the admins hear no
  ``agent_pool_empty`` for it.
- ``takes`` is the claim's side: the run being claimed is a KG run that a node of the shared pool may take.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import and_, any_, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.runs.models.placement import POOL_KEY, normalized

log = logging.getLogger(__name__)

KINDS = ("review",)  # the kinds of a KG run: a review with no repo (lens kg_questions or kg_init)


@dataclass(frozen=True)
class Shared:
    """The shared KG pool: its id and name, its owner and the taints its runs tolerate."""

    pool_id: int
    pool: str
    owner_id: int
    taints: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{POOL_KEY}={self.pool}".lower()


def _no_repo(project_id):
    pr = tables.project_repos
    return ~exists().where(pr.c.project_id == project_id)


async def shared_pool(conn: AsyncConnection) -> Shared | None:
    """The pool a hub admin named the shared KG pool; None while none is."""
    p = tables.worker_pools
    query = select(p.c.id, p.c.name, p.c.owner_id, p.c.taints).where(p.c.kg_shared)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else Shared(row.id, row.name, row.owner_id, tuple(row.taints or ()))


async def for_project(conn: AsyncConnection, project_id: int) -> Shared | None:
    """The shared KG pool when it may take the KG runs of project ``project_id``: the project has no repo and its
    creator owns the pool. None otherwise."""
    shared = await shared_pool(conn)
    if shared is None:
        return None
    pj = tables.projects
    query = select(pj.c.id).where(pj.c.id == project_id, pj.c.created_by == shared.owner_id, _no_repo(pj.c.id))
    return shared if (await conn.scalar(query)) is not None else None


async def serve(conn: AsyncConnection, shared: Shared, project_id: int) -> None:
    """Add project ``project_id`` to the projects of the shared pool and of each of its live nodes, the ones it does
    not have yet; audited once, when the pool gains it."""
    pp, wp, w = tables.worker_pool_projects, tables.worker_projects, tables.workers
    added = await conn.execute(
        insert(pp)
        .values(pool_id=shared.pool_id, project_id=project_id)
        .on_conflict_do_nothing()
        .returning(pp.c.project_id)
    )
    nodes = select(w.c.id, literal(project_id)).where(w.c.pool_id == shared.pool_id, w.c.revoked_at.is_(None))
    await conn.execute(insert(wp).from_select(["worker_id", "project_id"], nodes).on_conflict_do_nothing())
    if added.scalar_one_or_none() is None:
        return
    name = await conn.scalar(select(tables.projects.c.name).where(tables.projects.c.id == project_id))
    target = f"pool:{shared.pool_id} name={shared.pool} project={name}"
    await audit.record(
        conn, actor_id=None, token_id=None, action=audit.WORKER_POOL_KG_SHARED, target=target, project_id=project_id
    )
    log.info("the shared KG pool serves a project", extra={"pool_id": shared.pool_id, "project_id": project_id})


def _kg_run(values: dict) -> bool:
    repos = values.get("repos")
    return values.get("kind") in KINDS and not (isinstance(repos, list) and repos)


async def placed(conn: AsyncConnection, values: dict) -> dict | None:
    """``values`` of a run to insert, kept on the shared KG pool when its selector names that pool and the pool may
    take it (``for_project``); None for any other run, which goes to its agent's pool."""
    if not _kg_run(values):
        return None
    shared = await for_project(conn, values["project_id"])
    if shared is None or shared.label not in normalized(values.get("selector")):
        return None
    tolerations = normalized([*(values.get("tolerations") or ()), *shared.taints])
    return {**values, "pinned_worker_id": None, "selector": normalized(values["selector"]), "tolerations": tolerations}


def takes(worker_id: int):
    """Whether the run being claimed (``tables.runs``, correlated) is a KG run that worker ``worker_id``, a node of the
    shared KG pool, may take: of a kind of ``KINDS`` with no repo, of a project with no repo created by the pool's
    owner, and asking pool=<the shared pool>."""
    r, w, p, pj = tables.runs, tables.workers.alias("kg_node"), tables.worker_pools.alias("kg_pool"), tables.projects
    pool_label = func.lower(literal(f"{POOL_KEY}=") + p.c.name)
    pool = (
        select(p.c.id)
        .join_from(w, p, p.c.id == w.c.pool_id)
        .join(pj, pj.c.created_by == p.c.owner_id)
        .where(w.c.id == worker_id, p.c.kg_shared, pj.c.id == r.c.project_id, pool_label == any_(r.c.selector))
        .correlate(r)
    )
    return and_(
        r.c.kind.in_(KINDS),
        func.coalesce(func.jsonb_array_length(r.c.repos), 0) == 0,
        _no_repo(r.c.project_id),
        exists(pool),
    )


__all__ = ["KINDS", "Shared", "for_project", "placed", "serve", "shared_pool", "takes"]
