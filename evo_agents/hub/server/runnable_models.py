"""The models of provider accounts a member may run in a project: GET /v1/projects/{project}/runnable-models, what the
dispatch form offers (``evo_agents.hub.gateway_runs``, ``docs/credentials.md``, section "Provider accounts").

Each model is named as a run asks for it, ``<prefix>/<model>``, with the runtimes that run it through the gateway and
the accounts that serve it: the caller's own provider accounts bound to the project and those shared with them for it
that a run of theirs may use now (``gateway.service.accounts.usable_accounts``, on any worker: live, not paused, not past
their end, a share that lends now). A model of none of them is not listed; a dispatch of one is 422, saying why.
Never a key: no query here reads one.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models.runnable_models import RunnableModel as RunnableModel
from evo_agents.hub.server.runs.service import runnable_models as service
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/projects",
    tags=["provider-accounts"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)


@router.get("/{project}/runnable-models", response_model=list[RunnableModel])
async def list_runnable_models(
    request: Request, project: ProjectName, user: CurrentUser, response: Response
) -> list[RunnableModel]:
    """The models of provider accounts the caller may run in ``project``, through the model gateway."""
    found = await service.list_runnable(request.app.state.engine, user, project)
    response.headers.update(NO_STORE)
    return found
