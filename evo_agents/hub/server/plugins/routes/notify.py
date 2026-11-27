"""A notice from an isolated plugin to the members of a project (``docs/plugins.md``, section Permissions).

POST /v1/plugin-api/projects/{p}/notifications takes the token of a plugin's backend (``Authorization: Bearer evp_...``)
and no other credential, as the rest of /v1/plugin-api/ (``task_dispatch``). No token, a malformed one, or one that is
revoked or whose plugin is not installed or is turned off gets 401; a project that did not enable the plugin, or
whose grant lacks ``notifications:send`` among what the installed version still asks for, gets 403 (checked at each
call); a body that is wrong, or a ``link`` outside the plugin's pages and the project, gets 422.

``to`` names logins and/or the roles ``admins`` (the project's admins), ``writers`` (its writers and admins) and
``members`` (everyone who holds a grant). Each of them with a grant on the project gets one notification of kind notice,
notice_kind ``plugin_notice``, whose details name the plugin, its version, the ``topic`` and the ``key``; the
notifications go to the outbox in the transaction of the call (``NOTIFIER.notify``), so the web's Inbox and each
channel of the member receive them as any notice. A login without a grant is dropped and named in the answer. The
route has no rate limit.

A call with a ``key`` is made once: the first stores its answer in plugin_notice_keys with the notifications, and a
later call of the same plugin in the same project with the same key creates nothing and answers the first one's result.
The audit row ``plugin.notify`` names the plugin, the project and the number of recipients, never the title or body.
"""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugins.models.notify import PluginNoticeIn, PluginNoticeSent
from evo_agents.hub.server.plugins.service.notify import check_body, send_as
from evo_agents.hub.server.task_dispatch import plugin_of_request

REFUSALS = {code: {"model": ErrorBody} for code in (401, 403, 422)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


@router.post("/projects/{project}/notifications", status_code=201, response_model=PluginNoticeSent, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def send_notice(
    request: Request, response: Response, project: ProjectName, body: PluginNoticeIn
) -> PluginNoticeSent:
    """Send a notice to members of the project, as the plugin whose token the request carries."""
    check_body(body)
    sent = await send_as(request.app.state.engine, partial(plugin_of_request, request), project, body)
    if sent.duplicate:
        response.status_code = 200
    return sent
