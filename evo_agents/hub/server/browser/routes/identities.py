"""The browser identities of a project: /v1/projects/{project}/browser-identities (plan agent-browser, criterion a2).

A browser identity is a Chromium profile kept logged in to some sites, which runs of the project use on the one worker
that holds it (``evo_agents.hub.tables_browser``). A member's identity is theirs: they create it with the writer role
and only they see, use and delete it. A project's identity is the admins' to create and delete, and every writer may
attach it to a run. Its worker is the caller's own worker, not revoked, or a shared one, serves the project, and has
the feature browser: its daemon starts Chromium (``evo_agents.worker.browser``).

These routes deal in metadata alone: no answer, log line or audit row holds a cookie, a profile, a key or a hash of
one. The profiles themselves are sealed and kept by ``evo_agents.hub.server.browser.service.profiles``; deleting an identity
takes its keys from the row and records its blobs for deletion. Every write is an audit row (``audit.BROWSER_*``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.browser.models.identities import REFUSALS, BrowserIdentity, IdentityId
from evo_agents.hub.server.browser.service import identities as service
from evo_agents.hub.server.browser.service.shapes import BrowserIdentityCreate
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["browser"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/browser-identities", response_model=list[BrowserIdentity], responses=REFUSALS)
async def list_browser_identities(
    request: Request, response: Response, project: ProjectName, user: CurrentUser
) -> list[BrowserIdentity]:
    """The project's identities and your own in it, never another member's, and its agents' for an admin; metadata
    only."""
    async with request.app.state.engine.connect() as conn:
        found = await service.list_identities(conn, user, project)
    response.headers.update(NO_STORE)
    return found


@router.post("/{project}/browser-identities", status_code=201, response_model=BrowserIdentity, responses=REFUSALS)
async def create_browser_identity(
    request: Request, response: Response, project: ProjectName, body: BrowserIdentityCreate, user: CurrentUser
) -> BrowserIdentity:
    """Create an identity with no profile yet (version 0): your own with the writer role, or the project's or an
    agent's as an admin. It is logged in later, on its worker."""
    service.check_create(body)
    async with request.app.state.engine.begin() as conn:
        created = await service.create_identity(conn, user, project, body)
    response.headers.update(NO_STORE)
    return created


@router.delete(
    "/{project}/browser-identities/{identity_id}", status_code=204, response_class=Response, responses=REFUSALS
)
async def delete_browser_identity(
    request: Request, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> Response:
    """Delete an identity: its member, or an admin for the project's. Its keys go from the hub and its profiles from
    the blob store; 409 while a run holds it."""
    async with request.app.state.engine.begin() as conn:
        await service.delete_identity(conn, user, project, identity_id)
    return Response(status_code=204)


@router.post("/{project}/browser-identities/{identity_id}/default", response_model=BrowserIdentity, responses=REFUSALS)
async def default_browser_identity(
    request: Request, response: Response, project: ProjectName, identity_id: IdentityId, user: CurrentUser
) -> BrowserIdentity:
    """Make an agent's identity the one its profile hands to every run under it, in place of the one that was: an admin
    of the project. 409 for an identity no agent owns."""
    async with request.app.state.engine.begin() as conn:
        found = await service.make_default(conn, user, project, identity_id)
    response.headers.update(NO_STORE)
    return found
