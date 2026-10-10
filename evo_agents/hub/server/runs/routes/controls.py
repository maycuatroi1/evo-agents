"""The owner's controls of a run: POST /v1/projects/{p}/runs/{id}/cancel, takeover, handback, approve and rerun
(``service.controls``)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.runs.models import REFUSALS, Run, RunId
from evo_agents.hub.server.runs.service.controls import approve_run, ask_control, cancel_run, rerun_run
from evo_agents.hub.server.security import CurrentUser

router = APIRouter()


@router.post("/{project}/runs/{run_id}/cancel", response_model=Run, responses=REFUSALS)
async def cancel(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Cancel a queued run or one in review at once; ask the worker holding a held run to stop it."""
    return await cancel_run(request.app.state.engine, user, project, run_id)


@router.post("/{project}/runs/{run_id}/takeover", response_model=Run, responses=REFUSALS)
async def takeover(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Ask the worker to stop the agent at the end of its turn and resume its session in a terminal, where a person
    drives it (the run becomes interactive)."""
    return await ask_control(request.app.state.engine, user, project, run_id, audit.RUN_TAKEOVER)


@router.post("/{project}/runs/{run_id}/handback", response_model=Run, responses=REFUSALS)
async def handback(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Ask the worker to close the terminal and let the agent go on headless in the same session (the run becomes
    running again)."""
    return await ask_control(request.app.state.engine, user, project, run_id, audit.RUN_HANDBACK)


@router.post("/{project}/runs/{run_id}/approve", response_model=Run, responses=REFUSALS)
async def approve(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Approve a run in review: the run is done, and so is its step."""
    return await approve_run(request.app.state.engine, user, project, run_id)


@router.post("/{project}/runs/{run_id}/rerun", status_code=201, response_model=Run, responses=REFUSALS)
async def rerun(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser) -> Run:
    """Queue the step of a run that ended again, with the same runtime, model, mode, approval, timeout and worker, at
    the plan's current revision."""
    return await rerun_run(request.app.state.engine, user, project, run_id)
