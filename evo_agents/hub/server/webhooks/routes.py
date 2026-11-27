"""The outbound webhooks of a project, for its admin (``evo_agents.hub.webhook_events``, ``docs/hub.md``, section
"Outbound webhooks"). Each passes the request's parts to ``service.hooks``.

GET /v1/projects/{p}/webhooks lists them; POST adds one, an https URL and the events it takes, and answers its signing
secret, in that answer alone; PATCH changes its URL, events or switch; POST .../rotate-secret gives it a new secret,
in that answer alone; DELETE removes it with its outbox; GET .../deliveries lists its latest deliveries. Each route is
the admin's of the project: a webhook's URL may hold a receiver's key. The secret is sealed under EVO_HUB_SECRETS_KEY
(``webhook_events.webhook_aad``), and no answer but those two, no log line and no audit row holds it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.webhooks.models import (
    NO_STORE,
    Webhook,
    WebhookCreate,
    WebhookCreated,
    WebhookDelivery,
    WebhookSecret,
    WebhookUpdate,
)
from evo_agents.hub.server.webhooks.service import hooks

router = APIRouter(prefix="/v1/projects", tags=["webhooks"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/webhooks", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def list_webhooks(request: Request, project: ProjectName, user: CurrentUser) -> list[Webhook]:
    """The project's outbound webhooks, with how many deliveries of each wait and failed."""
    return await hooks.list_webhooks(request.app.state.engine, user, project)


@router.post(
    "/{project}/webhooks",
    status_code=201,
    responses={
        403: {"model": ErrorBody},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        503: {"model": ErrorBody},
    },
    dependencies=[route_autonomy.K8],
)
async def create_webhook(
    request: Request, response: Response, project: ProjectName, body: WebhookCreate, user: CurrentUser
) -> WebhookCreated:
    """Add an outbound webhook; its signing secret is in this answer alone."""
    state = request.app.state
    made = await hooks.create_webhook(state.engine, state.sealer, state.config, user, project, body)
    response.headers.update(NO_STORE)
    return made


@router.patch(
    "/{project}/webhooks/{webhook_id}",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def update_webhook(
    request: Request, project: ProjectName, webhook_id: int, body: WebhookUpdate, user: CurrentUser
) -> Webhook:
    """Change a webhook's URL, events or switch; a webhook turned off fails what it left pending."""
    return await hooks.update_webhook(request.app.state.engine, user, project, webhook_id, body)


@router.post(
    "/{project}/webhooks/{webhook_id}/rotate-secret",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 503: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def rotate_secret(
    request: Request, response: Response, project: ProjectName, webhook_id: int, user: CurrentUser
) -> WebhookSecret:
    """Give a webhook a new signing secret, in this answer alone; the old one signs nothing from now on."""
    state = request.app.state
    made = await hooks.rotate_secret(state.engine, state.sealer, state.config, user, project, webhook_id)
    response.headers.update(NO_STORE)
    return made


@router.delete(
    "/{project}/webhooks/{webhook_id}",
    status_code=204,
    response_class=Response,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def delete_webhook(request: Request, project: ProjectName, webhook_id: int, user: CurrentUser) -> Response:
    """Delete a webhook with its outbox."""
    await hooks.delete_webhook(request.app.state.engine, user, project, webhook_id)
    return Response(status_code=204)


@router.get(
    "/{project}/webhooks/{webhook_id}/deliveries", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
)
async def list_deliveries(
    request: Request, project: ProjectName, webhook_id: int, user: CurrentUser
) -> list[WebhookDelivery]:
    """A webhook's latest deliveries, newest first: their state, tries, last HTTP status and kind of error."""
    return await hooks.list_deliveries(request.app.state.engine, user, project, webhook_id)
