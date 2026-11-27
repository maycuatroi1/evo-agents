"""The route GET /v1/projects/{project}/runs/{run_id}/accounts, the accounts a run took, which reads through
``evo_agents.hub.server.accounts.service.views``; that module says what it gives."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from evo_agents.hub.server.accounts.models.views import REFUSALS, RunAccount
from evo_agents.hub.server.accounts.service import views as service
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["accounts"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/runs/{run_id}/accounts", response_model=list[RunAccount], responses=REFUSALS)
async def run_accounts(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> list[RunAccount]:
    """The accounts run ``run_id`` took, by variable, and each usage limit it hit; for the member who dispatched it."""
    async with request.app.state.engine.begin() as conn:
        return await service.accounts_of_run(conn, user, project, run_id, sink)
