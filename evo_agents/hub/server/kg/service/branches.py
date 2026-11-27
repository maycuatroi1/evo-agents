"""Draft branches of a project's graph (``evo_agents.hub.server.kg_branches``): who may read and change them, and one
transaction per request around ``evo_agents.hub.kg_branches``, audited as ``kg.branch`` once a write is done.

Each function raises ``kg_branches.BranchError`` for a refused operation, which the route answers; nothing it began
is kept then. The writes take ``may_change``, the check of the named graphs' routes (``kg_graphs.may_change``), from
the route, as a service does not import a route module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_branches as store
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.branches import BranchesOut, BranchOut, BranchResult
from evo_agents.hub.server.kg.service.web import require_reader
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.projects import project_access

MayChange = Callable[[AsyncConnection, object, object], Awaitable[None]]


async def _audit(conn: AsyncConnection, user, access, project: str, name: str, kind: str, ops: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_BRANCH,
        target=f"{project} branch={name} kind={kind} ops={ops}",
        project_id=access.project_id,
    )


async def reader(conn: AsyncConnection, user, access) -> store.Reader:
    """The audience and ceiling (level, location) of ``user``'s grant, as the kg reads of ``kg_web`` take them."""
    audience = await reader_audience(conn, user)
    ceiling = access.rules.grant_label(access.max_level, audience)
    return audience, None if ceiling is None else (ceiling.level, ceiling.location)


async def branches(engine: AsyncEngine, user, project: str, status: str | None) -> BranchesOut:
    """The draft branches of the project's graph, newest first."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        found = await store.branches(conn, access.project_id, status)
    return BranchesOut(branches=[BranchOut(**b.as_dict()) for b in found])


async def create(engine: AsyncEngine, user, project: str, name: str, may_change: MayChange) -> BranchOut:
    """Open a draft branch of the project's graph from main."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        made = await store.create(conn, access.project_id, name, user.user_id)
        await _audit(conn, user, access, project, made.name, "create", 0)
    return BranchOut(**made.as_dict())


async def show(engine: AsyncEngine, user, project: str, name: str) -> BranchOut:
    """One draft branch of the project's graph."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        found = await store.get(conn, access.project_id, name)
    return BranchOut(**found.as_dict())


async def apply(engine: AsyncEngine, user, project: str, name: str, ops: list, may_change: MayChange) -> BranchResult:
    """Apply ``ops`` to an open branch, all or none; main does not change."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        writer = await reader(conn, user, access)
        done = await store.apply(conn, access.project_id, name, ops, user.user_id, writer)
        await _audit(conn, user, access, project, name, "apply", done["applied"])
    return BranchResult(**done)


async def diff(engine: AsyncEngine, user, project: str, name: str) -> BranchResult:
    """The nodes and edges a branch adds, changes and removes from main, of what the grant reads."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        seen = await reader(conn, user, access)
        return BranchResult(**await store.diff(conn, access.project_id, name, seen))


async def check(engine: AsyncEngine, user, project: str, name: str) -> BranchResult:
    """The ontology rules a branch breaks, those main does not, its conflicts with main, and whether it would merge."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        seen = await reader(conn, user, access)
        return BranchResult(**await store.check(conn, access.project_id, name, seen))


async def merge(engine: AsyncEngine, user, project: str, name: str, may_change: MayChange) -> BranchResult:
    """Merge an open branch into main in one transaction."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        done = await store.merge(conn, access.project_id, name, user.user_id)
        await _audit(conn, user, access, project, name, "merge", done["ops"])
    return BranchResult(**done)


async def drop(engine: AsyncEngine, user, project: str, name: str, may_change: MayChange) -> BranchResult:
    """Drop an open branch and its operations; main does not change."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        done = await store.drop(conn, access.project_id, name)
        await _audit(conn, user, access, project, name, "drop", done["ops"])
    return BranchResult(**done)
