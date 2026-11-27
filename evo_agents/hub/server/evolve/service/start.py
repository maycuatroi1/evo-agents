"""The start of an evolution by a member with the writer role (POST /v1/projects/{project}/curator/evolve): what it
checks, the tasks it picks once, and its run, queued now when the night shift's gate is open to a run a member asks
for, else the reason the evolution waits."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, evolve
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.changes.repository.views import charter_of
from evo_agents.hub.server.evolve.models import EVOLVE_START, EvolveStart, EvolveView, reason_text
from evo_agents.hub.server.evolve.repository import evolutions
from evo_agents.hub.server.evolve.service import views
from evo_agents.hub.server.evolve.service.runs import pick_tasks, queue_run, settings_of
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.systems.service import curation as system_curation

log = logging.getLogger(__name__)


async def _checked_git(conn: AsyncConnection, access: ProjectAccess, body: EvolveStart, globs: list[str]) -> None:
    if not await evolutions.has_repo(conn, access.project_id, body.repo):
        raise Unprocessable(f"project {access.name} has no repo {body.repo}")
    outside = [path for path in body.paths if not evolve.evolvable(body.repo or "", path, globs)]
    if outside:
        raise Unprocessable(
            f"{', '.join(outside[:5])} of {body.repo}: no harness file the charter's evolve.evolvable_paths names; "
            "nothing was started",
        )


async def _checked_proposal(conn: AsyncConnection, access: ProjectAccess, proposal_id: int | None) -> None:
    if proposal_id is None:
        return
    if not await evolutions.has_proposal(conn, access.project_id, proposal_id):
        raise NotFound(f"project {access.name} has no proposal #{proposal_id}")


async def _gate(conn: AsyncConnection, project_id: int):
    """(due, word, gate) of the project's night shift for an evolution its owner asks for now: at any hour, beside the
    schedule's other runs, as an asked-for KG optimize run (``kg_optimize._gate``)."""
    from evo_agents.hub.server.curator.repository.shift import due_schedules
    from evo_agents.hub.server.curator.service.shift import gate_of  # it imports the routes of the night shift

    due = await evolutions.due_schedule(conn, due_schedules(None), project_id)
    if due is None:
        return None, "none", None
    word, gate = await gate_of(conn, due, any_hour=True, beside=True, on_demand=True)
    return due, word, gate


async def _checked(conn: AsyncConnection, access: ProjectAccess, body: EvolveStart, charter: dict):
    """The charter's evolve settings, once the evolution ``body`` asks for may start."""
    settings = settings_of(charter)
    if not settings.enabled:
        raise Conflict(f"the charter of project {access.name} does not enable evolve: nothing was started")
    if body.target == "git":
        await _checked_git(conn, access, body, settings.evolvable_paths)
    elif body.system_id is not None and body.agent is not None:  # EvolveStart holds both for target agent_config
        await system_curation.evolve_target(conn, access.project_id, body.system_id, body.agent)
    await _checked_proposal(conn, access, body.proposal_id)
    return settings


async def start(conn: AsyncConnection, user: Principal, project: str, body: EvolveStart) -> int:
    """Start an evolution (see the package's docstring); its id. Its run is queued now when the gate is open."""
    from evo_agents.hub.server.build_now import GATE_REFUSALS  # it imports the routes of the night shift
    from evo_agents.hub.server.night_duty import on_duty

    access = await project_access(conn, user, project)
    check_dispatcher(access)
    await check_seat(conn, user)
    charter = await charter_of(conn, access.project_id)
    settings = await _checked(conn, access, body, charter)
    tasks = await pick_tasks(conn, access.project_id, body.repo, body.paths, settings)
    values = body.model_dump(exclude={"target"}) | {"target": body.target, "project_id": access.project_id}
    values |= {"label": access.push_label(), "budget": settings.budget.model_dump(), "tasks": tasks}
    values |= {"runtime": (charter.get("builder") or {}).get("runtime") or curator.DEFAULT_RUNTIME}
    values |= {"model": (charter.get("builder") or {}).get("model"), "created_by": user.user_id}
    evolve_id = await evolutions.inserted(conn, values)
    target = f"{access.name} evolve:{evolve_id} target:{body.target}"
    await Auditor.of(conn, user, access).record(EVOLVE_START, target)
    due, word, gate = await _gate(conn, access.project_id)
    if gate is None:
        why = GATE_REFUSALS.get(word, word)
        if word == "pool":
            why = (await on_duty(conn, due))[1] or why
        await evolutions.set_row(conn, evolve_id, reason=reason_text(f"it waits for the night shift: {why}"))
    else:
        row = await evolutions.one(conn, evolve_id, lock=True)
        await queue_run(conn, due, gate, row, asked_by=user.login)
    log.info("evolution started", extra={"project": access.name, "evolve_id": evolve_id, "target": body.target})
    return evolve_id


async def start_evolution(engine: AsyncEngine, user: Principal, project: str, body: EvolveStart) -> EvolveView:
    """Start an evolution (``start``) in a transaction of its own: the evolution as it was just started."""
    async with UnitOfWork(engine) as conn:
        evolve_id = await start(conn, user, project, body)
        return await views.started(conn, project, evolve_id)
