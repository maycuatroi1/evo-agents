"""What the routes of a project's Loop (``routes``) read and write, each in a transaction of its own: its state and the
reset of its breaker, its work items and goals, the decision board, the baseline of a goal and the adoption of a plan.
Who may read is checked here (a grant on the project); the rest is the Loop's modules. The graph a baseline shows is
read by the route between two of these transactions, so the one that writes never waits on it.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.loop import adopt, baseline, board, chain, items
from evo_agents.hub.server.loop import breaker as loop_breaker
from evo_agents.hub.server.loop.board_items import DecisionBoard
from evo_agents.hub.server.loop.chain import LoopItemChain
from evo_agents.hub.server.loop.items import LoopGoal, LoopItems
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds


async def _view(conn, project_id: int, project: str) -> dict:
    return {"project": project, **await loop_breaker.status(conn, project_id)}


async def _reader(conn, user, project: str, what: str):
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading {what} of project {access.name} needs a grant on it")
    return access


# The Loop's state and its breaker


async def read_loop(engine: AsyncEngine, user: Principal, project: str) -> dict:
    """The fields of the project's Loop (the route's ``ProjectLoop``) for anyone with a grant on it, in a transaction of
    its own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the Loop of project {access.name} needs a grant on it")
        return await _view(conn, access.project_id, access.name)


async def resume(engine: AsyncEngine, user: Principal, project: str) -> dict:
    """Reset the tripped breaker of the project's Loop (``breaker.resume``), in a transaction of its own: the fields of
    the Loop as it stands then."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await loop_breaker.resume(conn, user, access)
        return await _view(conn, access.project_id, access.name)


# Its work items, its goals and the decision board


async def read_items(engine: AsyncEngine, user: Principal, project: str, sink: str | None, **found) -> LoopItems:
    """The work items of the project's Loop (``items.listed`` with ``found``) for a reader, in a transaction of its
    own."""
    async with UnitOfWork(engine) as conn:
        access = await _reader(conn, user, project, "the work items")
        reading = await board.reading_of(conn, access, sink)
        return await items.listed(conn, reading, **found)


async def read_item(
    engine: AsyncEngine, user: Principal, project: str, item_id: int, sink: str | None
) -> LoopItemChain:
    """A work item with its chain (``chain.shown``) for a reader, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await _reader(conn, user, project, "a work item")
        return await chain.shown(conn, await board.reading_of(conn, access, sink), item_id)


async def read_goals(engine: AsyncEngine, user: Principal, project: str) -> list[LoopGoal]:
    """Each goal of the project with its figures (``items.goal_figures``) for a reader, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await _reader(conn, user, project, "the goals")
        return await items.goal_figures(conn, access.project_id)


async def read_board(engine: AsyncEngine, user: Principal, project: str, sink: str | None) -> DecisionBoard:
    """The project's decision board (``board.gather``) for anyone with a grant on it, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the decision board of project {access.name} needs a grant on it")
        return await board.gather(conn, access, sink)


# The baseline of a goal, and the adoption of a plan


async def baseline_of(engine: AsyncEngine, user: Principal, project: str, ref: str, *, writing: bool = False):
    """(access, goal id, goal code, codes of its use cases) for the baseline of goal ``ref``, in a transaction of its
    own; 403 for a writer who is not an admin of the project (or is the agent of a run)."""
    async with UnitOfWork(engine) as conn:
        access = await _reader(conn, user, project, "the baseline of a goal")
        if writing and (user.acts_for_run is not None or not holds(access.role, "admin")):
            raise HTTPException(403, f"setting the baseline of a goal needs the admin role on project {access.name}")
        goal_id, code, _ = await baseline.goal_of(conn, access.project_id, ref)
        return access, goal_id, code, await baseline.refs(conn, goal_id)


async def read_baseline(
    engine: AsyncEngine, access: ProjectAccess, goal_id: int, code: str, found, why: str | None
) -> baseline.SrsBaseline:
    """``baseline.view`` with the graph's evidence ``found``, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await baseline.view(conn, access.name, goal_id, code, found, why)


async def fix_baseline(
    engine: AsyncEngine,
    user: Principal,
    access: ProjectAccess,
    goal_id: int,
    code: str,
    body: baseline.BaselineFix,
    found,
    why: str | None,
) -> baseline.SrsBaseline:
    """``baseline.fix``, in a transaction of its own: the baseline as it stands then, with the builds cancelled."""
    async with UnitOfWork(engine) as conn:
        done = await baseline.fix(conn, user, access, goal_id, code, body, found)
        shown = await baseline.view(conn, access.name, goal_id, code, found, why)
        return shown.model_copy(update={"cancelled": done.cancelled})


async def confirm_baseline(
    engine: AsyncEngine, user: Principal, access: ProjectAccess, goal_id: int, code: str, found, why: str | None
) -> baseline.SrsBaseline:
    """``baseline.confirm``, in a transaction of its own: the baseline as it stands then."""
    async with UnitOfWork(engine) as conn:
        await baseline.confirm(conn, user, access, goal_id, code, found)
        return await baseline.view(conn, access.name, goal_id, code, found, why)


async def adopt_plan(engine: AsyncEngine, user: Principal, project: str, body: adopt.LoopAdopt) -> adopt.LoopAdopted:
    """``adopt.adopt`` for a reader of the project, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        access = await _reader(conn, user, project, "the Loop")
        return await adopt.adopt(conn, user, access, body)
