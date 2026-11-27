"""A project's data loss prevention policy, its routes, and its enforcement before a model call leaves the hub
(``evo_agents.hub.dlp``, ``docs/hub.md``, section "Data loss prevention").

GET /v1/projects/{p}/dlp-policy gives a reader the policy and its counts; PUT sets it, for the project's admin; POST
/v1/projects/{p}/dlp/inspect gives a reader what the policy makes of a text, as a quick check that counts nothing.

``enforce`` applies the policy of a run's project to the body of a model call, for the gateway route and the hub
runtime: it gives the body to send, or raises ``Blocked``, and counts in dlp_counts what it redacted or blocked, by
rule kind. A log line names the kinds and their counts, never a matched value, and so does a refusal.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.hub_admin.models.dlp_policy import BLOCKED as BLOCKED
from evo_agents.hub.server.hub_admin.models.dlp_policy import Blocked as Blocked
from evo_agents.hub.server.hub_admin.models.dlp_policy import DlpCount as DlpCount
from evo_agents.hub.server.hub_admin.models.dlp_policy import DlpFinding as DlpFinding
from evo_agents.hub.server.hub_admin.models.dlp_policy import DlpInspectIn as DlpInspectIn
from evo_agents.hub.server.hub_admin.models.dlp_policy import DlpInspection as DlpInspection
from evo_agents.hub.server.hub_admin.models.dlp_policy import DlpPolicy as DlpPolicy
from evo_agents.hub.server.hub_admin.models.dlp_policy import DlpPolicyWrite as DlpPolicyWrite
from evo_agents.hub.server.hub_admin.models.dlp_policy import Keyword as Keyword
from evo_agents.hub.server.hub_admin.models.dlp_policy import Kind as Kind
from evo_agents.hub.server.hub_admin.models.dlp_policy import Mode as Mode
from evo_agents.hub.server.hub_admin.models.dlp_policy import what as what
from evo_agents.hub.server.hub_admin.service import dlp_policy as service
from evo_agents.hub.server.hub_admin.service.dlp_policy import count as count
from evo_agents.hub.server.hub_admin.service.dlp_policy import enforce as enforce
from evo_agents.hub.server.hub_admin.service.dlp_policy import enforce_json as enforce_json
from evo_agents.hub.server.hub_admin.service.dlp_policy import policy_of as policy_of
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["dlp"], responses={401: {"model": ErrorBody}})


# The routes


@router.get("/{project}/dlp-policy", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def show_policy(request: Request, project: ProjectName, user: CurrentUser) -> DlpPolicy:
    """The project's DLP policy, off when none was set, and the counts of what it redacted and blocked."""
    return await service.show(request.app.state.engine, user, project)


@router.put(
    "/{project}/dlp-policy",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K12],
)
async def write_policy(request: Request, project: ProjectName, body: DlpPolicyWrite, user: CurrentUser) -> DlpPolicy:
    """Set the project's DLP policy, for its admin: mode off, redact or block, the rule kinds, and the keywords of
    confidential contracts it adds to the built-in ones."""
    return await service.write(request.app.state.engine, user, project, body)


@router.post("/{project}/dlp/inspect", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def inspect(request: Request, project: ProjectName, body: DlpInspectIn, user: CurrentUser) -> DlpInspection:
    """What the project's policy makes of ``text``: the text it sends, whether it blocks it, and its matches by kind.
    A quick check: nothing is stored or counted."""
    return await service.inspect(request.app.state.engine, user, project, body.text)
