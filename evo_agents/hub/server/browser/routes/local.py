"""A browser identity logged in on the worker's own machine, outside any run: /v1/worker/browser-identities (plan
agent-browser, criterion a9).

The owner of a machine with a screen runs ``evo-agents worker browser login <identity>`` there: Chromium opens with the
identity's profile, the owner signs in and closes the window, and the command sends the profile back. Both routes
answer the worker's token alone, in the name of the member who owns the worker, who must hold the role the identity
asks (writer for their own or the project's, admin for an agent's) on its project.

POST .../login opens the identity: it names it by project, owner kind and name, and creates it, held by this worker,
when it has none and says its domains. An identity of another worker, held by a run, disabled, or with a login session
waiting is 409. The answer is the lease of a run's identity (``browser_leases.BrowserProfileLease``) with the profile
opened, and the audit row is browser_identity.lease with " local" in its target.

PUT .../{identity}/profile takes the profile back as the next version, as a run's save does (413 past
MAX_ARCHIVE_BYTES, 422 for an archive that is not one, 409 for a save that lost or while a run holds the identity), and
sets the identity active on this worker. The audit row is browser_identity.save.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.browser.models.leases import REFUSALS, BrowserProfileSaved, ProfileRoute
from evo_agents.hub.server.browser.models.local import LocalId, LocalLogin, LocalLoginAsk
from evo_agents.hub.server.browser.service import local as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.models.browser import CHROMIUM_VERSION
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
upload_router = APIRouter(
    prefix="/v1/worker", tags=["worker protocol"], route_class=ProfileRoute, responses={401: {"model": ErrorBody}}
)


@router.post("/browser-identities/login", response_model=LocalLogin, responses=REFUSALS)
async def open_local_login(request: Request, body: LocalLoginAsk, user: CurrentUser) -> LocalLogin:
    """The identity, with its profile, for a login on this worker's own machine; created, held by this worker, when it
    does not exist yet."""
    return await service.open_local_login(request, user, body)


@upload_router.put(
    "/browser-identities/{identity_id}/profile",
    response_model=BrowserProfileSaved,
    responses={**REFUSALS, 413: {"model": ErrorBody}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "the archive of the profile, as `evo_agents.hub.browser_archive.pack` makes it",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def save_local_profile(
    request: Request,
    identity_id: LocalId,
    user: CurrentUser,
    base_version: Annotated[int, Query(ge=0, le=MAX_ID, description="the version the login started from")],
    chromium_version: Annotated[
        str | None, Query(pattern=CHROMIUM_VERSION, description="the worker's Chromium, as it says its version")
    ] = None,
) -> BrowserProfileSaved:
    """Seal the profile a login on the worker's machine leaves as the identity's next version."""
    return await service.save_local_profile(request, user, identity_id, base_version, chromium_version)
