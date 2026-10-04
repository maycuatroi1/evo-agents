"""Signing in on the web with GitHub's web application flow, and the session cookie it ends with.

GET /v1/auth/web/login makes a state and a PKCE S256 verifier, keeps both in a signed cookie that lives for
LOGIN_WINDOW seconds and goes only to the callback, and redirects to GitHub with redirect_uri set to
EVO_HUB_PUBLIC_URL + CALLBACK. GET /v1/auth/web/callback compares the state GitHub returns with the cookie's in
constant time, exchanges the code with the client secret and the verifier, asks GitHub whose token it got, drops
that token, and sets the session cookie (HttpOnly, Secure, SameSite=Lax, Path=/) on a new ``web`` row of tokens
before redirecting to /. A state that does not match answers 400 and creates nothing; either way the login cookie
is spent. POST /v1/auth/web/logout revokes the session and deletes the cookie, and GET /v1/auth/web/csrf hands out
the X-Evo-CSRF value the session's writes need.

Every route here answers 503 until the client id and secret, the session secret and the public URL are all set;
the CLI's device flow does not depend on any of them but the client id.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.auth import NO_STORE, github_errors, sign_in, sign_out
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.security import (
    CSRF_HEADER,
    WEB,
    CurrentUser,
    b64,
    csrf_token,
    delete_session_cookie,
    same,
    set_session_cookie,
    sign,
    unsign,
)

log = logging.getLogger(__name__)

CALLBACK = "/v1/auth/web/callback"
LOGIN_COOKIE = "evo_hub_login"
LOGIN_WINDOW = 600  # seconds between leaving for GitHub and coming back
LOGIN_PURPOSE = "web-login"
SCOPE = "read:user"  # reading the login needs nothing more
AFTER_LOGIN = "/"
RESTART = "start again at /v1/auth/web/login"
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")

router = APIRouter(prefix="/v1/auth/web", tags=["auth"])
UNCONFIGURED = {503: {"model": ErrorBody, "description": "web sign-in is not configured"}}


class Csrf(BaseModel):
    csrf: str
    header: str = CSRF_HEADER


def web_config(request: Request) -> HubConfig:
    config: HubConfig = request.app.state.config
    missing = config.web_login_missing()
    if missing:
        raise HTTPException(503, f"web sign-in is not configured on this hub: {', '.join(missing)} not set")
    return config


WebConfig = Annotated[HubConfig, Depends(web_config)]


def _set_login_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        LOGIN_COOKIE, value, max_age=LOGIN_WINDOW, path=CALLBACK, secure=True, httponly=True, samesite="lax"
    )


def _refuse(request: Request, status: int, message: str) -> Response:
    response = error_response(request, status, message, headers=NO_STORE)
    response.delete_cookie(LOGIN_COOKIE, path=CALLBACK, secure=True, httponly=True, samesite="lax")
    return response


@router.get("/login", status_code=302, response_class=RedirectResponse, responses=UNCONFIGURED)
async def web_login(config: WebConfig) -> RedirectResponse:
    """Leave for GitHub's authorize page with a fresh state and PKCE challenge."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 characters, inside RFC 7636's 43 to 128
    query = urlencode(
        {
            "client_id": config.github_client_id,
            "redirect_uri": config.public_url + CALLBACK,
            "scope": SCOPE,
            "state": state,
            "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "allow_signup": "false",
        }
    )
    response = RedirectResponse(f"{config.github_url}/login/oauth/authorize?{query}", status_code=302, headers=NO_STORE)
    payload = {"state": state, "verifier": verifier}
    _set_login_cookie(response, sign(config.session_secret, LOGIN_PURPOSE, payload, LOGIN_WINDOW))
    return response


@router.get(
    "/callback",
    status_code=303,
    response_class=RedirectResponse,
    responses={400: {"model": ErrorBody}, 502: {"model": ErrorBody}, **UNCONFIGURED},
)
async def web_callback(
    request: Request,
    config: WebConfig,
    code: Annotated[str | None, Query(max_length=512)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    """Where GitHub sends the browser back: check the state, then sign in and set the session cookie."""
    saved = unsign(config.session_secret, LOGIN_PURPOSE, request.cookies.get(LOGIN_COOKIE) or "")
    expected = saved.get("state") if saved else None
    if not isinstance(expected, str) or not state or not same(state, expected):
        log.warning("web sign-in refused: the state does not match the login cookie", extra={"cookie": bool(saved)})
        return _refuse(request, 400, f"the sign-in state does not match: {RESTART}")
    if error or not code:
        reason = error if error and _ERROR_CODE.fullmatch(error) else "no code"
        log.info("web sign-in not authorized on GitHub", extra={"github_error": reason})
        return _refuse(request, 400, f"GitHub did not authorize the sign-in ({reason}): {RESTART}")
    github = request.app.state.github
    try:
        with github_errors(400, RESTART):
            # The GitHub token only tells whose browser this is; it is never stored.
            verifier = str(saved.get("verifier", ""))
            github_token = await github.exchange_code(code, verifier, config.public_url + CALLBACK)
            user = await github.user(github_token)
    except HTTPException as exc:
        return _refuse(request, exc.status_code, exc.detail)
    async with request.app.state.pool.connection() as conn:
        signed = await sign_in(conn, config, user, WEB, None)
    response = RedirectResponse(AFTER_LOGIN, status_code=303, headers=NO_STORE)
    set_session_cookie(response, signed.token)
    response.delete_cookie(LOGIN_COOKIE, path=CALLBACK, secure=True, httponly=True, samesite="lax")
    return response


@router.get("/csrf", response_model=Csrf, responses=UNCONFIGURED)
async def csrf(response: Response, config: WebConfig, user: CurrentUser) -> Csrf:
    """The X-Evo-CSRF value of this web session, for every POST, PUT, PATCH and DELETE it makes."""
    if user.kind != WEB:
        raise HTTPException(400, "a CSRF token belongs to a web session; a Bearer request needs none")
    response.headers.update(NO_STORE)
    return Csrf(csrf=csrf_token(config.session_secret, user.token_hash))


@router.post("/logout", status_code=204, response_class=Response, responses=UNCONFIGURED)
async def web_logout(request: Request, config: WebConfig, user: CurrentUser) -> Response:
    """Revoke this web session and delete its cookie."""
    if user.kind != WEB:
        raise HTTPException(400, "this ends a web session; a machine token signs out with `evo-agents hub logout`")
    await sign_out(request, user)
    response = Response(status_code=204)
    delete_session_cookie(response)
    return response
