"""App sessions: a first-party app signs in its own users, who have no GitHub account, as members of the hub.

A hub admin registers the app with POST /v1/admin/auth-apps {name}, which answers its secret, ``evk_...``, once; the
hub keeps it sealed under EVO_HUB_SECRETS_KEY (``sealing.auth_app_aad``), lists the apps with GET and revokes one with
DELETE /v1/admin/auth-apps/{name}, which revokes every session it opened too.

POST /v1/auth/app-session {app, subject, login} trades the identity the app's enterprise sign-in gives a person for an
interactive session of that one member. The request is public (``security.PUBLIC_PATHS``): its credential is the app's
signature, SIGNATURE_HEADER ``sha256=<hex>``, the HMAC-SHA256 under the app's secret of TIMESTAMP_HEADER, a ``.`` and the
raw body, made within WINDOW seconds of the hub's clock. An unknown or revoked app and a wrong or stale signature get
the same 401. ``subject`` is the stable identity of the person at the app (the subject of its sign-in): the same app
and subject find the same users row every time (``tables.app_identities``). The first sign-in creates the row with no
GitHub account (``github_id`` NULL) and the login ``<login>_<app>``: no GitHub login holds ``_``, so a GitHub sign-in
never claims it, and a hub admin can grant that login a role before the person first signs in, as for a GitHub login.
The login stays as it was made; another subject asking for a login taken gets 409.

The answer is a session ``eva_...`` (``security.APP``), sent as ``Authorization: Bearer``: a web session of the member
(``tokens`` kind web, host ``app:<name>``), so the hub applies to it everything it applies to that person signed in on
the web: their own grants, the labels of their chats, their autonomy and ceilings, the quota of their accounts, and the
rules that only a web session passes (``runs.web_only_steering``, a worker's ``dispatch_from`` web). It needs no CSRF
header, being no cookie. It lives as any web session (``security.TOKEN_TTL`` without use) and ends with POST
/v1/auth/logout. Every sign-in adds an audit row ``auth.login`` of target ``app:<name>``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.credentials.models.sealing import Sealer
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.identity.models.app_sessions import APP_NAME as APP_NAME
from evo_agents.hub.server.identity.models.app_sessions import HANDLE as HANDLE
from evo_agents.hub.server.identity.models.app_sessions import HOST_PREFIX as HOST_PREFIX
from evo_agents.hub.server.identity.models.app_sessions import REFUSED as REFUSED
from evo_agents.hub.server.identity.models.app_sessions import SECRET_BYTES as SECRET_BYTES
from evo_agents.hub.server.identity.models.app_sessions import SECRET_PREFIX as SECRET_PREFIX
from evo_agents.hub.server.identity.models.app_sessions import SIGNATURE_HEADER as SIGNATURE_HEADER
from evo_agents.hub.server.identity.models.app_sessions import TIMESTAMP_HEADER as TIMESTAMP_HEADER
from evo_agents.hub.server.identity.models.app_sessions import WINDOW as WINDOW
from evo_agents.hub.server.identity.models.app_sessions import AppSessionRequest as AppSessionRequest
from evo_agents.hub.server.identity.models.app_sessions import AuthApp as AuthApp
from evo_agents.hub.server.identity.models.app_sessions import AuthAppCreate as AuthAppCreate
from evo_agents.hub.server.identity.models.app_sessions import AuthAppCreated as AuthAppCreated
from evo_agents.hub.server.identity.models.auth import NO_STORE, SignedIn
from evo_agents.hub.server.identity.service import app_sessions as service
from evo_agents.hub.server.identity.service.app_sessions import signature as signature
from evo_agents.hub.server.security import AdminUser

router = APIRouter(prefix="/v1/auth", tags=["auth"])
admin_router = APIRouter(prefix="/v1/admin/auth-apps", tags=["admin"])
AppName = Annotated[str, Path(pattern=APP_NAME)]


def _sealer(request: Request) -> Sealer:
    sealer = request.app.state.sealer
    if sealer is None:
        config: HubConfig = request.app.state.config
        missing = ", ".join(config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, f"app sessions need the hub's secrets key: set {missing}")
    return sealer


@router.post(
    "/app-session",
    status_code=201,
    response_model=SignedIn,
    responses={401: {"model": ErrorBody}, 409: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def app_session(
    request: Request,
    response: Response,
    body: AppSessionRequest,
    timestamp: Annotated[str | None, Header(alias=TIMESTAMP_HEADER)] = None,
    given: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> SignedIn:
    """Trade the identity an app's sign-in gives a person for a session of that member, sent as Bearer."""
    sealer, config = _sealer(request), request.app.state.config
    raw = await request.body()
    signed = await service.app_session(request.app.state.engine, sealer, config, body, raw, timestamp, given)
    response.headers.update(NO_STORE)
    return signed


@admin_router.get("", response_model=list[AuthApp], responses={403: {"model": ErrorBody}})
async def list_apps(request: Request, user: AdminUser) -> list[AuthApp]:
    """The apps registered, revoked ones too, without their secrets."""
    return await service.list_apps(request.app.state.engine)


@admin_router.post(
    "",
    status_code=201,
    response_model=AuthAppCreated,
    responses={403: {"model": ErrorBody}, 409: {"model": ErrorBody}, 503: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def create_app(request: Request, response: Response, body: AuthAppCreate, user: AdminUser) -> AuthAppCreated:
    """Register an app that signs its users in; its secret is in this answer alone."""
    sealer = _sealer(request)
    created = await service.create_app(request.app.state.engine, sealer, body.name, user)
    response.headers.update(NO_STORE)
    return created


@admin_router.delete(
    "/{name}",
    status_code=204,
    response_class=Response,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def revoke_app(request: Request, name: AppName, user: AdminUser) -> Response:
    """Revoke an app: it signs nobody in again, and every session it opened ends now."""
    await service.revoke_app(request.app.state.engine, name, user)
    return Response(status_code=204)
