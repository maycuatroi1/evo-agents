"""The owner's view of a run's leases: GET /v1/projects/{p}/runs/{id}/credentials, the leases ``credentials`` handed
out, given back or not (``docs/credentials.md``, section "What a run got").

GET /v1/projects/{p}/runs/{id}/credentials lists every lease the run got, given back or not, for the member who
dispatched it: the secret's name or ``github-app:<account>``, provider, kind, target (the variable, or the origins it
answered for), the worker, and when it was issued, ends and was revoked; never a value, sealed or not. The run must be
one the caller may read (``runs.readable_run``: 404 otherwise); another member, a hub admin included, gets 403, since
the leases name the owner's secrets as GET /v1/secrets does to the owner alone. A lease of an account of the run's
agent (``agent_accounts``) is named ``@<handle>:<name>``, which no secret of the owner can be. A run on a provider
account's model (``evo_agents.hub.gateway_runs``) shows provider and kind ``gateway``: its key of the gateway as
``gateway:<model>``, and each provider account that key reaches by the account's name, the target being the model. A
run that runs as its owner shows the lease of its token of the hub as ``run-access:<owner>``, provider run-access and
kind hub (``run_access``), and a KG run the lease of its own as ``kg-run:<project>`` (``kg_run_access``). Each
lease names the agent profile and version it was handed out under (``evo_agents.hub.profiles``), null for one from
before schema 0030.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.models.run_leases import RunLease as RunLease
from evo_agents.hub.server.runs.service import run_leases as service
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@router.get("/{project}/runs/{run_id}/credentials", response_model=list[RunLease], responses=REFUSALS)
async def run_credentials(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> list[RunLease]:
    """Every lease the run got, given back or not, without a value; for the member who dispatched it."""
    return await service.run_credentials(request.app.state.engine, user, project, run_id, sink)
