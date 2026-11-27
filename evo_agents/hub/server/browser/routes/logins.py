"""Logging a browser identity in (plan agent-browser, criterion a7): POST /v1/projects/{project}/browser-identities/{id}/login
queues a login session (a run of kind browser, ``evo_agents.hub.browser_login``), and .../disable and .../enable switch
an identity off and on. ``notify_needs_login`` tells an identity's owner it waits for a new login.

A login session is pinned to the worker that holds the identity or, for a Move (``worker`` names another one), to that
worker; the identity moves there when the session saves. The dispatcher opens it in the live view of the run and ends it
with Save (``POST .../runs/{id}/finish``) or cancels it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.browser.models.identities import REFUSALS, BrowserIdentity, IdentityId
from evo_agents.hub.server.browser.models.logins import BrowserLogin
from evo_agents.hub.server.browser.service import logins as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["browser"], responses={401: {"model": ErrorBody}})


@router.post(
    "/{project}/browser-identities/{identity_id}/login", status_code=201, response_model=Run, responses=REFUSALS
)
async def login_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, body: BrowserLogin, user: CurrentUser
) -> Run:
    """Queue a login session of an identity: Chromium on its profile, on its worker, for at most 30 minutes, which you
    open in the live view in human mode and end with Save (the profile is sealed, its version raised) or by cancelling
    it (nothing is saved). With ``worker`` it is a Move: the session runs there and the identity moves after Save.
    409 while another run holds the identity or a session of it waits, and for a disabled one."""
    async with request.app.state.engine.begin() as conn:
        return await service.queue_login(conn, user, project, identity_id, body)


@router.post("/{project}/browser-identities/{identity_id}/disable", response_model=BrowserIdentity, responses=REFUSALS)
async def disable_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> BrowserIdentity:
    """Disable an identity: no run attaches it and its login session is refused until it is enabled; its profile is
    kept. Its member, or an admin for the project's; 409 while a run holds it."""
    async with request.app.state.engine.begin() as conn:
        return await service.set_status(conn, user, project, identity_id, disable=True)


@router.post("/{project}/browser-identities/{identity_id}/enable", response_model=BrowserIdentity, responses=REFUSALS)
async def enable_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> BrowserIdentity:
    """Enable a disabled identity: active when it has a saved profile, else needs_login."""
    async with request.app.state.engine.begin() as conn:
        return await service.set_status(conn, user, project, identity_id, disable=False)
