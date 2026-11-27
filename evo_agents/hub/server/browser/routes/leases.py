"""The profiles of a run's browser identities, to and from its worker: /v1/worker/runs/{id}/browser-profile (plan
agent-browser, criterion a4).

Each route answers only the worker that holds the run, in the held states (404 for any other), while the member who
dispatched it holds the writer role on its project (403), and only for an identity of the run's that the run holds
(``browser_holds``) on that worker (409 otherwise) and its dispatcher may use: their own, or the project's (403). A
disabled identity is 403. Every answer is in the dispatcher's name, with the worker's token, in the audit log.

POST .../browser-profile hands the worker one identity: its name, its domains, the version its profile is at and the
profile itself, opened (``browser_profiles.open_profile``) and in base64, or null for one never saved. A profile the
hub cannot open (its key or blob altered or gone) is null too, and sets the identity needs_login. The audit row is
browser_identity.lease.

PUT .../browser-profile/{identity} takes the profile back as the run ends: the archive (``browser_archive``) as the
body, application/octet-stream of at most MAX_ARCHIVE_BYTES (413 past it, read before anything is parsed), with the
version the run started from. A corrupt archive is 422, a save that lost (the identity saved meanwhile, or no longer
the run's) 409 (``save_profile``), and the version the identity had stays in each case. A new version is the audit
row browser_identity.save.

POST .../browser-profile/{identity}/needs-login is the worker's word that its Chromium could not open the profile,
or that a newer Chromium saved it: the identity, unless disabled, waits for a new login (status needs_login, the
audit row browser_identity.needs_login), and the run goes on without it. No answer, log line or audit row holds a
profile, a cookie, a domain or a key, save the domains the lease hands the worker.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.browser.models.leases import (
    REFUSALS,
    BrowserIdentityState,
    BrowserNeedsLogin,
    BrowserProfileAsk,
    BrowserProfileLease,
    BrowserProfileSaved,
    IdentityId,
    ProfileRoute,
)
from evo_agents.hub.server.browser.service import leases as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.runs.models.browser import CHROMIUM_VERSION
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
upload_router = APIRouter(
    prefix="/v1/worker", tags=["worker protocol"], route_class=ProfileRoute, responses={401: {"model": ErrorBody}}
)


@worker_router.post("/runs/{run_id}/browser-profile", response_model=BrowserProfileLease, responses=REFUSALS)
async def lease_browser_profile(
    request: Request, run_id: RunId, body: BrowserProfileAsk, user: CurrentUser
) -> BrowserProfileLease:
    """One of the run's browser identities, with its profile: what its Chromium opens with."""
    return await service.lease(request, user, run_id, body.identity_id)


@upload_router.put(
    "/runs/{run_id}/browser-profile/{identity_id}",
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
async def save_browser_profile(
    request: Request,
    run_id: RunId,
    identity_id: IdentityId,
    user: CurrentUser,
    base_version: Annotated[int, Query(ge=0, le=MAX_ID, description="the version the run's profile started from")],
    chromium_version: Annotated[
        str | None, Query(pattern=CHROMIUM_VERSION, description="the worker's Chromium, as it says its version")
    ] = None,
) -> BrowserProfileSaved:
    """Seal the profile the run's Chromium leaves as the identity's next version, while the run holds it."""
    return await service.save(request, user, run_id, identity_id, base_version, chromium_version)


@worker_router.post(
    "/runs/{run_id}/browser-profile/{identity_id}/needs-login",
    response_model=BrowserIdentityState,
    responses=REFUSALS,
)
async def browser_needs_login(
    request: Request, run_id: RunId, identity_id: IdentityId, body: BrowserNeedsLogin, user: CurrentUser
) -> BrowserIdentityState:
    """The run's Chromium could not use the identity's profile: it waits for a new login."""
    return await service.needs_login(request.app.state.engine, user, run_id, identity_id, body.cause)
