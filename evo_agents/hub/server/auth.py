"""Signing in from the CLI, signing out, and who the caller is.

GET /v1/auth/config gives the CLI the public client id it needs for GitHub's device flow, which runs between the
CLI and GitHub alone. POST /v1/auth/github takes the GitHub token the flow ends with, asks GitHub whose it is and
answers with a new machine token of the hub; the GitHub token is never stored or logged and is gone when the
request ends. When the client secret is configured, GitHub must first confirm that the token was issued to this
hub's OAuth App, so a token of another app or a personal token cannot sign anyone in.

A sign-in finds the user by GitHub id, so a login renamed on GitHub keeps its grants, and otherwise claims a row an
admin granted to that login before its first sign-in. Every sign-in and sign-out adds an audit row.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.identity.models.auth import NO_STORE as NO_STORE
from evo_agents.hub.server.identity.models.auth import USER_RETRIES as USER_RETRIES
from evo_agents.hub.server.identity.models.auth import AuthConfig as AuthConfig
from evo_agents.hub.server.identity.models.auth import GitHubLogin as GitHubLogin
from evo_agents.hub.server.identity.models.auth import GrantInfo as GrantInfo
from evo_agents.hub.server.identity.models.auth import SignedIn as SignedIn
from evo_agents.hub.server.identity.models.auth import TokenInfo as TokenInfo
from evo_agents.hub.server.identity.models.auth import WhoAmI as WhoAmI
from evo_agents.hub.server.identity.service import auth as service
from evo_agents.hub.server.identity.service.auth import github_errors as github_errors
from evo_agents.hub.server.identity.service.auth import sign_in as sign_in
from evo_agents.hub.server.identity.service.auth import sign_out as sign_out
from evo_agents.hub.server.identity.service.auth import upsert_user as upsert_user
from evo_agents.hub.server.security import WEB, CurrentUser, delete_session_cookie

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.get("/config", response_model=AuthConfig, responses={503: {"model": ErrorBody}})
async def auth_config(request: Request) -> AuthConfig:
    config: HubConfig = request.app.state.config
    if not config.github_client_id:
        raise HTTPException(503, "sign-in is not configured on this hub (EVO_HUB_GITHUB_CLIENT_ID is not set)")
    return AuthConfig(github_client_id=config.github_client_id, web_login=not config.web_login_missing())


@router.post(
    "/github",
    status_code=201,
    response_model=SignedIn,
    responses={401: {"model": ErrorBody}, 502: {"model": ErrorBody}},
)
async def github_login(body: GitHubLogin, request: Request, response: Response) -> SignedIn:
    """Trade the GitHub token of the CLI's device flow for a machine token of the hub."""
    config: HubConfig = request.app.state.config
    github = request.app.state.github
    host = body.host.strip()
    if not host:
        raise HTTPException(422, "host must name the machine")
    signed = await service.github_login(request.app.state.engine, config, github, body.github_token, host)
    response.headers.update(NO_STORE)
    return signed


@router.get("/whoami", response_model=WhoAmI, responses={401: {"model": ErrorBody}})
async def whoami(request: Request, user: CurrentUser) -> WhoAmI:
    return await service.whoami(request.app.state.engine, user)


@router.post("/logout", status_code=204, response_class=Response, responses={401: {"model": ErrorBody}})
async def logout(request: Request, user: CurrentUser) -> Response:
    """Revoke the token (or web session) this request came with."""
    await sign_out(request.app.state.engine, user)
    response = Response(status_code=204)
    if user.kind == WEB:
        delete_session_cookie(response)
    return response
