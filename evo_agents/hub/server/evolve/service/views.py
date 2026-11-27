"""An evolution as its owner reads it (``view_of``), with its candidates (``candidates_of``), and the evolutions a
caller may read through a sink (``evolutions_of``, ``evolution_of``); the routes read them with ``listed`` and
``shown``, each in a transaction of its own."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import evolve
from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.evolve.models import CandidateView, EvolveList, EvolveTaskView, EvolveView, FrontItem
from evo_agents.hub.server.evolve.repository import candidates, evolutions
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal


async def candidates_of(conn: AsyncConnection, evolve_id: int, *, components: bool) -> list[CandidateView]:
    return [
        CandidateView(
            id=row.id,
            parents=list(row.parents or []),
            origin=row.origin,
            iteration=row.iteration,
            digest=row.digest,
            scores=dict(row.scores or {}),
            cost_usd=row.cost_usd,
            turns=row.turns,
            on_front=row.on_front,
            components=dict(row.components) if components else None,
            asi=evolve.asi_of(row.traces),
            created_at=row.created_at,
        )
        for row in await candidates.of_evolution(conn, evolve_id)
    ]


def view_of(project: str, row, candidates: list[CandidateView] | None = None) -> EvolveView:
    """An evolution as its owner reads it: its tasks without their verify."""
    tasks = [
        {"kind": task["kind"], "id": task["id"], "ref": task.get("ref") or evolve.task_ref(task["kind"], task["id"])}
        | {"repo": task.get("repo")}
        for task in row.tasks or []
        if isinstance(task, dict) and task.get("kind") in evolve.TASK_KINDS
    ]
    return EvolveView(
        id=row.id,
        project=project,
        run_id=row.run_id,
        target=row.target,
        repo=row.repo,
        paths=list(row.paths or []),
        system_id=row.system_id,
        agent=row.agent,
        state=row.state,
        runtime=row.runtime,
        model=row.model,
        budget=dict(row.budget or {}),
        tasks=[EvolveTaskView.model_validate(task) for task in tasks],
        iterations=row.iterations,
        frontier=[FrontItem.model_validate(item) for item in row.frontier or []],
        best_candidate_id=row.best_candidate_id,
        proposal_id=row.proposal_id,
        change_id=row.change_id,
        reason=row.reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
        candidates=candidates,
    )


async def started(conn: AsyncConnection, project: str, evolve_id: int) -> EvolveView:
    """Evolution ``evolve_id`` as it was just started: no candidate yet."""
    return view_of(project, await evolutions.one(conn, evolve_id), [])


async def evolutions_of(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, *, state: str | None = None, limit: int = 20
) -> list:
    """The project's evolutions the caller may read through ``sink``, the newest first."""
    rows = await evolutions.listed(conn, access.project_id, state, limit)
    return [row for row in rows if access.visible(row.label, plan_service.read_sink(access, sink) or "")]


async def evolution_of(conn: AsyncConnection, access: ProjectAccess, evolve_id: int, sink: str | None):
    """Evolution ``evolve_id`` of the project; 404 when there is none the caller may read."""
    row = await evolutions.of_project(conn, access.project_id, evolve_id)
    if row is None or not access.visible(row.label, plan_service.read_sink(access, sink) or ""):
        raise NotFound(f"project {access.name} has no evolution #{evolve_id}")
    return row


async def listed(
    engine: AsyncEngine, user: Principal, project: str, sink: str | None, *, state: str | None, limit: int
) -> EvolveList:
    """The project's evolutions a reader may read through ``sink``, the newest first."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        rows = await evolutions_of(conn, access, sink, state=state, limit=limit)
    return EvolveList(project=access.name, evolutions=[view_of(access.name, row) for row in rows])


async def shown(engine: AsyncEngine, user: Principal, project: str, evolve_id: int, sink: str | None) -> EvolveView:
    """Evolution ``evolve_id`` for a reader, with each candidate and its components."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        row = await evolution_of(conn, access, evolve_id, sink)
        candidates = await candidates_of(conn, row.id, components=True)
    return view_of(access.name, row, candidates)
