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

import logging
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import legacy
from evo_agents.hub.server import audit
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable, GitHubUser
from evo_agents.hub.server.security import (
    LOGIN_HINT,
    MACHINE,
    WEB,
    WWW_AUTHENTICATE,
    CurrentUser,
    Principal,
    delete_session_cookie,
    issue_token,
    revoke_token,
)

log = logging.getLogger(__name__)

NO_STORE = {"Cache-Control": "no-store"}
USER_RETRIES = 3  # a sign-in racing another sign-in of the same new account looks again

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class AuthConfig(BaseModel):
    github_client_id: str = Field(description="public client id of the hub's GitHub OAuth App")
    web_login: bool = Field(description="whether GET /v1/auth/web/login is configured on this hub")


class GitHubLogin(BaseModel):
    github_token: str = Field(min_length=1, max_length=1024, description="the token GitHub's device flow ended with")
    host: str = Field(
        min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$", description="host name of the machine"
    )


class SignedIn(BaseModel):
    token: str = Field(description="the hub token; shown once, the hub keeps only its SHA-256")
    token_id: int
    login: str
    admin: bool
    expires_at: datetime


class TokenInfo(BaseModel):
    id: int
    kind: str
    host: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime


class GrantInfo(BaseModel):
    project: str
    role: str
    max_level: str


class WhoAmI(BaseModel):
    login: str
    admin: bool
    token: TokenInfo
    grants: list[GrantInfo]


@contextmanager
def github_errors(refused_status: int, hint: str):
    """GitHub's answers as API errors: refused becomes ``refused_status``, unavailable becomes 502."""
    try:
        yield
    except GitHubRefused as exc:
        headers = WWW_AUTHENTICATE if refused_status == 401 else None
        raise HTTPException(refused_status, f"{exc}: {hint}", headers=headers) from None
    except GitHubUnavailable as exc:
        raise HTTPException(502, str(exc)) from None


async def _one(conn, sql: str, params=()):
    return await (await legacy(conn, sql, params)).fetchone()


async def _move_aside(conn, user_id: int) -> None:
    """Give up the login of a row whose GitHub account was renamed since: ``_`` cannot occur in a GitHub login, so
    the new name collides with nobody, and the account gets its real login back when it signs in again."""
    await legacy(
        conn, "UPDATE users SET login = left(login, 80) || '_' || coalesce(github_id, id) WHERE id = %s", (user_id,)
    )


async def upsert_user(conn, user: GitHubUser) -> tuple[int, str]:
    """The id and login of the users row for a GitHub account, created or brought up to date."""
    for _ in range(USER_RETRIES):
        own = await _one(conn, "SELECT id FROM users WHERE github_id = %s FOR UPDATE", (user.id,))
        holder = await _one(
            conn, "SELECT id, github_id FROM users WHERE lower(login) = lower(%s) FOR UPDATE", (user.login,)
        )
        if holder is not None and holder[1] != user.id:
            holder_id, holder_github_id = holder
            if holder_github_id is None and own is None:  # granted before the first sign-in: claim it
                await legacy(
                    conn,
                    "UPDATE users SET github_id = %s, login = %s, last_seen_at = now() WHERE id = %s",
                    (user.id, user.login, holder_id),
                )
                return holder_id, user.login
            if holder_github_id is None:  # granted to the new login of a known account: merge the grants into it
                await legacy(
                    conn,
                    "INSERT INTO grants (user_id, project_id, role, max_level, granted_by, granted_at) "
                    "SELECT %s, project_id, role, max_level, granted_by, granted_at FROM grants WHERE user_id = %s "
                    "ON CONFLICT (user_id, project_id) DO NOTHING",
                    (own[0], holder_id),
                )
                await legacy(conn, "DELETE FROM users WHERE id = %s", (holder_id,))
            else:
                await _move_aside(conn, holder_id)
        if own is not None:
            await legacy(conn, "UPDATE users SET login = %s, last_seen_at = now() WHERE id = %s", (user.login, own[0]))
            return own[0], user.login
        row = await _one(
            conn,
            "INSERT INTO users (login, github_id, last_seen_at) VALUES (%s, %s, now()) ON CONFLICT DO NOTHING "
            "RETURNING id",
            (user.login, user.id),
        )
        if row is not None:
            return row[0], user.login
    raise RuntimeError(f"could not settle the users row of GitHub account {user.id} after {USER_RETRIES} tries")


async def sign_in(conn, config: HubConfig, user: GitHubUser, kind: str, host: str | None) -> SignedIn:
    """A new token of ``kind`` for the GitHub ``user``, with its audit row, in the caller's transaction."""
    user_id, login = await upsert_user(conn, user)
    issued = await issue_token(conn, user_id, kind, host)
    target = f"machine:{host}" if kind == MACHINE else "web"
    await audit.record(conn, actor_id=user_id, token_id=issued.token_id, action=audit.LOGIN, target=target)
    log.info("signed in", extra={"login": login, "kind": kind, "token_id": issued.token_id})
    return SignedIn(
        token=issued.token,
        token_id=issued.token_id,
        login=login,
        admin=config.is_admin(login),
        expires_at=issued.expires_at,
    )


async def sign_out(request: Request, user: Principal) -> None:
    """Revoke the credential the request came with."""
    async with request.app.state.engine.begin() as conn:
        if await revoke_token(conn, user.token_id, user.user_id):
            target = audit.token_target(user.token_id)
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.LOGOUT, target=target)
    log.info("signed out", extra={"login": user.login, "kind": user.kind, "token_id": user.token_id})


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
    with github_errors(401, LOGIN_HINT):
        if config.github_client_id and config.github_client_secret:
            if not await github.issued_to_app(body.github_token):
                log.warning("sign-in refused: the GitHub token belongs to another app")
                raise GitHubRefused("the GitHub token was not issued to this hub's OAuth App")
        user = await github.user(body.github_token)
    async with request.app.state.engine.begin() as conn:
        signed = await sign_in(conn, config, user, MACHINE, host)
    response.headers.update(NO_STORE)
    return signed


@router.get("/whoami", response_model=WhoAmI, responses={401: {"model": ErrorBody}})
async def whoami(request: Request, user: CurrentUser) -> WhoAmI:
    async with request.app.state.engine.begin() as conn:
        token = await _one(
            conn,
            "SELECT id, kind, host, created_at, last_used_at, expires_at FROM tokens WHERE id = %s",
            (user.token_id,),
        )
        cursor = await legacy(
            conn,
            "SELECT p.name, g.role, g.max_level FROM grants g JOIN projects p ON p.id = g.project_id "
            "WHERE g.user_id = %s ORDER BY p.name",
            (user.user_id,),
        )
        grants = await cursor.fetchall()
    fields = ("id", "kind", "host", "created_at", "last_used_at", "expires_at")
    return WhoAmI(
        login=user.login,
        admin=user.admin,
        token=TokenInfo(**dict(zip(fields, token, strict=True))),
        grants=[GrantInfo(project=p, role=r, max_level=m) for p, r, m in grants],
    )


@router.post("/logout", status_code=204, response_class=Response, responses={401: {"model": ErrorBody}})
async def logout(request: Request, user: CurrentUser) -> Response:
    """Revoke the token (or web session) this request came with."""
    await sign_out(request, user)
    response = Response(status_code=204)
    if user.kind == WEB:
        delete_session_cookie(response)
    return response
