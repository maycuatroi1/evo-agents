"""Invite links: a hub admin makes a one-time link that gives a grant on a project, or the right to register one new
project, and a signed-in member accepts it. (In a self-managed project its admin makes a join link too, up to their
own role and max level: ``evo_agents.hub.server.project_admin``.)

POST /v1/admin/invites makes a link of kind ``join``, naming a project, a role and a max level of the project's ladder
as ``PUT /v1/admin/projects/{project}/grants/{login}`` takes them, or of kind ``register``, naming none. It lasts
``days``, DEFAULT_DAYS unless asked, MAX_DAYS at most, and ``for_login`` binds it to the one GitHub login that may
accept it, so a bound link can be posted where anyone reads it. Its token is ``evn_`` and 32 random bytes in base64url;
the hub keeps the token's hex SHA-256 (``security.hash_token``) and answers the token and the link, EVO_HUB_PUBLIC_URL
+ PAGE + the token, in that response alone. GET /v1/admin/invites lists the links, newest first, each with its status:
open, accepted, revoked or expired. POST /v1/admin/invites/{invite_id}/revoke revokes an open link, or the right an
accepted register link gave that no registration has used yet; any other link is 409. Those three routes are a hub
admin's alone: anyone else gets 403.

POST /v1/invites/preview is public: the web's page of a link reads, from the token alone, what the link gives, so a
visitor who has not signed in yet sees who invites them and to what. It names the admin who made the link, its kind,
status and expiry, and whether it is bound to a login, never which one; the project, role and max level only to a
visitor the link is for while it is open (anyone for an unbound link, the bound login alone, signed in, for a bound
one), and to the member who accepted it. It reads the session cookie when there is one, to say who is signed in, and
writes nothing.

POST /v1/invites/accept takes the token from a signed-in member, never from the agent of a run. The link's row is
locked first, so of two accepts at once one gives what the link gives and the other finds it accepted. A token the hub
does not know is 404; a link accepted, revoked or expired is 410, and one bound to another login 403, each saying why
and giving nothing. A join link gives its grant as a hub admin's grant does, except that it never lowers one the
member holds: the role and the max level are the higher of the held and the invited. A register link records the
right, which the member's next registration of a new project spends (``evo_agents.hub.server.projects``): in that
transaction the project is created, the member gets the admin role at the project's highest level
(``spend_register_right``), and the right is marked used. The right does not expire with the link; a hub admin revokes
it.

Making, accepting and revoking a link each write one audit row naming the link, "invite:<id> kind=<kind>", with the
project, role and max level of a join link and the login it is bound to, never the token; a join link's rows are in
its project.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.identity.models.auth import NO_STORE
from evo_agents.hub.server.identity.models.invites import ACCEPTED as ACCEPTED
from evo_agents.hub.server.identity.models.invites import ASK_AGAIN as ASK_AGAIN
from evo_agents.hub.server.identity.models.invites import DEFAULT_DAYS as DEFAULT_DAYS
from evo_agents.hub.server.identity.models.invites import EXPIRED as EXPIRED
from evo_agents.hub.server.identity.models.invites import JOIN as JOIN
from evo_agents.hub.server.identity.models.invites import MAX_DAYS as MAX_DAYS
from evo_agents.hub.server.identity.models.invites import OPEN as OPEN
from evo_agents.hub.server.identity.models.invites import PAGE as PAGE
from evo_agents.hub.server.identity.models.invites import PREFIX as PREFIX
from evo_agents.hub.server.identity.models.invites import REGISTER as REGISTER
from evo_agents.hub.server.identity.models.invites import REVOKED as REVOKED
from evo_agents.hub.server.identity.models.invites import TOKEN_BYTES as TOKEN_BYTES
from evo_agents.hub.server.identity.models.invites import InviteAccept as InviteAccept
from evo_agents.hub.server.identity.models.invites import InviteCreated as InviteCreated
from evo_agents.hub.server.identity.models.invites import InvitePreview as InvitePreview
from evo_agents.hub.server.identity.models.invites import InviteRequest as InviteRequest
from evo_agents.hub.server.identity.models.invites import InviteRow as InviteRow
from evo_agents.hub.server.identity.models.invites import Role as Role
from evo_agents.hub.server.identity.models.invites import Status as Status
from evo_agents.hub.server.identity.repository.invites import ROWS as ROWS
from evo_agents.hub.server.identity.service import invites as service
from evo_agents.hub.server.identity.service.invites import created as created
from evo_agents.hub.server.identity.service.invites import make_invite as make_invite
from evo_agents.hub.server.identity.service.invites import register_right as register_right
from evo_agents.hub.server.identity.service.invites import spend_register_right as spend_register_right
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.security import SESSION_COOKIE, WEB, AdminUser, CurrentUser, Principal, admin, authenticate

InviteId = Annotated[int, Path(ge=1, le=2**63 - 1)]

router = APIRouter(
    prefix="/v1/admin/invites",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)
accept_router = APIRouter(prefix="/v1/invites", tags=["invites"], responses={401: {"model": ErrorBody}})


@router.post(
    "",
    status_code=201,
    response_model=InviteCreated,
    responses={404: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def create(request: Request, response: Response, body: InviteRequest, user: AdminUser) -> InviteCreated:
    """Make a one-time invite link; the answer holds its token, which the hub shows this once."""
    made = await service.create(request.app.state.engine, request.app.state.config, user, body)
    response.headers.update(NO_STORE)
    return made


@router.get("", response_model=list[InviteRow], responses=PAGED)
async def list_invites(request: Request, page: Paging = WHOLE) -> list[InviteRow]:
    """Every invite link, newest first, with its status; never a token."""
    return await service.list_invites(request.app.state.engine, page)


@router.post(
    "/{invite_id}/revoke", response_model=InviteRow, responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
)
async def revoke(request: Request, invite_id: InviteId, user: AdminUser) -> InviteRow:
    """Revoke an open link, or the unused right an accepted register link gave."""
    return await service.revoke(request.app.state.engine, user, invite_id)


async def _viewer(request: Request) -> Principal | None:
    """Who is signed in on the web that sends ``request``: the live session of its cookie, or None."""
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        return None
    return await authenticate(request.app.state.engine, session, WEB, request.app.state.config)


@accept_router.post("/preview", response_model=InvitePreview, responses={404: {"model": ErrorBody}})
async def preview(request: Request, response: Response, body: InviteAccept) -> InvitePreview:
    """What an invite link gives, for its page: public, the token being the credential; never the login it is bound
    to, and its project and role only to someone it is for."""
    viewer = await _viewer(request)
    shown = await service.preview(request.app.state.engine, viewer, body.token)
    response.headers.update(NO_STORE)
    return shown


@accept_router.post(
    "/accept",
    response_model=InviteRow,
    responses={
        403: {"model": ErrorBody},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        410: {"model": ErrorBody, "description": "the link was accepted, revoked or has expired"},
    },
)
async def accept(request: Request, body: InviteAccept, user: CurrentUser) -> InviteRow:
    """Accept an invite link: a join link gives its grant, a register link the right to register one project."""
    return await service.accept(request.app.state.engine, user, body.token)
