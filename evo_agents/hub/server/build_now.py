"""Run plan on a Curator's plan: POST /v1/projects/{p}/curator/plans/{plan_id}/build (writer) queues the Builder of its
change now, beside the night shift's other runs, instead of waiting for them to end. It is the night shift's Builder in
every other way (``changes.queue_builder``, through ``curator.project_gate``): pinned to the worker on duty, as the
schedule's owner, on the change's branch alone, with the charter's runtime and the caps of the night that last opened,
at any hour; never while the night shift is paused or its circuit breaker tripped, past ``max_runs_per_night`` or the
night's budget, while a run of the plan is active, or before the hub checked the repo's ruleset. Each refusal is 409
with why, and nothing is queued. The run's events and the audit (curator.build_now) name the member who asked.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import judge
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.changes import change_of_plan, queue_builder
from evo_agents.hub.server.curator import project_gate
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import PlanId
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat
from evo_agents.hub.server.runs.service.views import run_view
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

BUILD_NOW = "curator.build_now"  # a member asked for a change's Builder now: "<project>/<plan> run:<id> change:<id>"

GATE_REFUSALS = {  # why the night shift's gate stays shut, by the word ``gate_of`` says
    "none": "the project has no night shift: an admin writes its charter first",
    "locked": "the night shift is queueing a run this very moment: ask again in a minute",
    "circuit": "its circuit breaker just paused the night shift: resume it first",
    "paused": "the night shift is paused: resume it first",
    "cancelled": "the night shift just cancelled a run of the night that outlived its window: ask again in a minute",
    "outside": "the night shift has no change of the Curator waiting",
    "owner": "the night shift's owner holds no writer grant on the project",
    "max_runs": "the night had max_runs_per_night runs already: raise it in the charter, or wait for the next night",
    "spent": "the night's budget is spent: raise night_budget_usd in the charter, or wait for the next night",
    "worker": "the worker on duty takes no run now (offline, draining, revoked, or not serving the project)",
}

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})


async def build_now(conn: AsyncConnection, user: Principal, project: str, plan_id: str, *, curator_app: bool) -> int:
    """Queue the Builder of the Curator's plan ``plan_id`` now (see the module's docstring); its run's id."""
    access = await project_access(conn, user, project)
    check_dispatcher(access)
    await check_seat(conn, user)
    await plan_routes._visible(conn, access, plan_id, None)  # 404 for a plan the caller may not read
    change = await change_of_plan(conn, access.project_id, plan_id) if judge.is_curator_plan_id(plan_id) else None
    if change is None:
        raise HTTPException(404, f"plan {plan_id} is not one of the Curator's plans in project {access.name}")
    if change.state != "planned":
        raise HTTPException(
            409, f"change #{change.id} of plan {plan_id} is {change.state}: its Builder has run; nothing was queued"
        )
    due, word, gate = await project_gate(conn, access.project_id)
    if gate is None:
        raise HTTPException(409, f"{GATE_REFUSALS.get(word, word)}; nothing was queued")
    run_id = await queue_builder(conn, due, gate, curator_app=curator_app, only=change.id)
    if run_id is None:
        why = (await change_of_plan(conn, access.project_id, plan_id)).reason or "it is not ready to build"
        raise HTTPException(409, f"change #{change.id} of plan {plan_id} waits: {why}; nothing was queued")
    text = f"{user.login} asked for it from Run plan: queued at once, beside the night shift's other runs."
    await write_event(conn, run_id, {"text": text, "change_id": change.id})
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=BUILD_NOW,
        target=f"{access.name}/{plan_id} run:{run_id} change:{change.id}",
        project_id=access.project_id,
    )
    log.info("curator builder queued now", extra={"project": access.name, "run_id": run_id, "change_id": change.id})
    return run_id


@router.post(
    "/{project}/curator/plans/{plan_id}/build",
    status_code=201,
    response_model=Run,
    responses={code: {"model": ErrorBody} for code in (403, 404, 409)},
)
async def build(request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser) -> Run:
    """Queue the Builder of the Curator's plan ``plan_id`` now, beside the night shift's other runs."""
    async with request.app.state.engine.begin() as conn:
        run_id = await build_now(conn, user, project, plan_id, curator_app=request.app.state.curator_app is not None)
        return await run_view(conn, run_id)
