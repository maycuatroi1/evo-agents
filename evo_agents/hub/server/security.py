"""Hub credentials and the check every /v1 request goes through.

A machine token is ``evh_`` and a web session ``evs_``, each followed by 32 random bytes in base64url. Postgres
keeps only the hex SHA-256 of either, in ``tokens`` with its kind; the plaintext exists only in the response that
issues it and on the holder's side. A token expires after TOKEN_TTL without use: using it pushes the expiry
TOKEN_TTL past now, but the row is written at most once per TOUCH_EVERY, so a busy token does not write on every
request. Revoked, expired and unknown credentials all get the same 401, which names ``evo-agents hub login``.

``Authenticate`` guards every path under /v1 except PUBLIC_PATHS and fails closed: a route added later needs a
credential unless it is listed there. A machine token arrives as ``Authorization: Bearer`` and a web session as
the SESSION_COOKIE cookie; each is accepted only through its own channel. A write (POST, PUT, PATCH, DELETE) made
with the cookie must also carry X-Evo-CSRF, an HMAC of the session under the session secret that GET
/v1/auth/web/csrf hands out; a Bearer request needs none, since a browser never adds that header by itself.
Every comparison of a secret value is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import Depends, HTTPException, Request
from starlette.responses import Response

from evo_agents.hub.server.errors import error_response

log = logging.getLogger(__name__)

MACHINE = "machine"
WEB = "web"
PREFIXES = {MACHINE: "evh_", WEB: "evs_"}
TOKEN_BYTES = 32
TOKEN_TTL = timedelta(days=90)  # without use; every use moves the expiry
TOUCH_EVERY = timedelta(days=1)  # at most one write of last_used_at and expires_at per token in this time
SESSION_COOKIE = "evo_hub_session"
CSRF_HEADER = "X-Evo-CSRF"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PROTECTED_PREFIX = "/v1/"
PUBLIC_PATHS = frozenset(
    {
        "/v1/health",
        "/v1/health/live",
        "/v1/openapi.json",
        "/v1/auth/config",
        "/v1/auth/github",
        "/v1/auth/web/login",
        "/v1/auth/web/callback",
    }
)
LOGIN_HINT = "run `evo-agents hub login`"
WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="evo-agents hub"'}

_TOKEN = re.compile(r"ev[hs]_[A-Za-z0-9_-]{43}")  # a prefix and 32 bytes of base64url without padding

# One statement: find a live token of the given kind, and when it was last written more than TOUCH_EVERY ago, push
# its expiry and mark the user seen. Two requests racing over a stale token write once: the second UPDATE waits
# for the first, then finds last_used_at fresh and changes nothing.
AUTHENTICATE = """
WITH found AS (
    SELECT t.id, t.user_id, u.login
      FROM tokens t JOIN users u ON u.id = t.user_id
     WHERE t.token_hash = %(hash)s AND t.kind = %(kind)s AND t.revoked_at IS NULL AND t.expires_at > now()
), touched AS (
    UPDATE tokens SET last_used_at = now(), expires_at = now() + %(ttl)s
     WHERE id = (SELECT id FROM found) AND (last_used_at IS NULL OR last_used_at <= now() - %(every)s)
    RETURNING id
), seen AS (
    UPDATE users SET last_seen_at = now()
     WHERE id = (SELECT user_id FROM found) AND EXISTS (SELECT 1 FROM touched)
)
SELECT id, user_id, login FROM found
"""


@dataclass(frozen=True)
class Principal:
    """Who made a request, and with which credential."""

    user_id: int
    login: str
    admin: bool  # the login is listed in EVO_HUB_ADMINS
    token_id: int
    kind: str  # MACHINE (a Bearer token) or WEB (the session cookie)
    token_hash: str

    def __repr__(self) -> str:
        return f"Principal(login={self.login!r}, token_id={self.token_id}, kind={self.kind!r})"


@dataclass(frozen=True)
class Issued:
    token: str  # the plaintext, handed to the caller once and never stored
    token_id: int
    expires_at: datetime

    def __repr__(self) -> str:
        return f"Issued(token_id={self.token_id})"


def new_token(kind: str) -> str:
    return PREFIXES[kind] + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(given: str, expected: str) -> bool:
    """Constant-time equality of two strings, any characters."""
    return hmac.compare_digest(given.encode(), expected.encode())


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, purpose: str, message: bytes) -> bytes:
    # The purpose keeps a value signed for one use from being accepted for another.
    return hmac.new(secret.encode(), purpose.encode() + b"\0" + message, hashlib.sha256).digest()


def sign(secret: str, purpose: str, payload: dict, ttl: int) -> str:
    """``payload`` plus an expiry ``ttl`` seconds from now, as base64url JSON and its HMAC-SHA256. Readable by
    whoever holds the value; only the signature keeps it from being changed."""
    body = b64(json.dumps({**payload, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    return f"{body}.{b64(_mac(secret, purpose, body.encode()))}"


def unsign(secret: str, purpose: str, value: str) -> dict | None:
    """The payload ``sign`` wrote, or None when the value was changed, signed for another purpose, or expired."""
    body, dot, mac = value.partition(".")
    if not dot or not same(mac, b64(_mac(secret, purpose, body.encode()))):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int) or payload["exp"] < time.time():
        return None
    return payload


def csrf_token(secret: str, session_hash: str) -> str:
    """The X-Evo-CSRF value of one web session: it changes with the session and cannot be made without the secret."""
    return b64(_mac(secret, "csrf", session_hash.encode()))


def set_session_cookie(response: Response, token: str) -> None:
    max_age = int(TOKEN_TTL.total_seconds())
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def delete_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


async def issue_token(conn, user_id: int, kind: str, host: str | None) -> Issued:
    token = new_token(kind)
    cursor = await conn.execute(
        "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) VALUES (%s, %s, %s, %s, now() + %s) "
        "RETURNING id, expires_at",
        (user_id, kind, hash_token(token), host, TOKEN_TTL),
    )
    token_id, expires_at = await cursor.fetchone()
    return Issued(token, token_id, expires_at)


async def revoke_token(conn, token_id: int, user_id: int) -> bool:
    """Revoke a token of ``user_id``; False when it has none by that id that is not revoked already."""
    cursor = await conn.execute(
        "UPDATE tokens SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL RETURNING id",
        (token_id, user_id),
    )
    return await cursor.fetchone() is not None


async def authenticate(pool, token: str, kind: str, config) -> Principal | None:
    """The principal behind a live ``token`` of ``kind``, or None. A malformed value never reaches the database."""
    if not _TOKEN.fullmatch(token) or not token.startswith(PREFIXES[kind]):
        return None
    digest = hash_token(token)
    params = {"hash": digest, "kind": kind, "ttl": TOKEN_TTL, "every": TOUCH_EVERY}
    async with pool.connection() as conn:
        row = await (await conn.execute(AUTHENTICATE, params)).fetchone()
    if row is None:
        return None
    token_id, user_id, login = row
    return Principal(user_id, login, config.is_admin(login), token_id, kind, digest)


def unauthorized(request: Request, message: str, *, clear_cookie: bool = False) -> Response:
    response = error_response(request, 401, message, headers=WWW_AUTHENTICATE)
    if clear_cookie:  # the browser should stop sending a session that will never work again
        delete_session_cookie(response)
    return response


class Authenticate:
    """Pure ASGI middleware: every request under /v1 outside PUBLIC_PATHS needs a live credential, and a write made
    with the session cookie needs its CSRF header. The principal goes to ``request.state.principal``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(PROTECTED_PREFIX) or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        refusal = await self._check(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _check(self, request: Request) -> Response | None:
        config = request.app.state.config
        authorization = request.headers.get("authorization")
        if authorization is not None:  # an explicit header wins; a bad one never falls back to the cookie
            scheme, _, token = authorization.strip().partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return unauthorized(request, f"the Authorization header must be `Bearer <token>`: {LOGIN_HINT}")
            kind, token = MACHINE, token.strip()
        else:
            token = request.cookies.get(SESSION_COOKIE)
            if not token:
                return unauthorized(request, f"sign-in required: {LOGIN_HINT}, or sign in on the web")
            kind = WEB
            if request.method in UNSAFE_METHODS:
                refusal = self._csrf_refusal(request, config, token)
                if refusal is not None:
                    return refusal
        try:
            principal = await authenticate(request.app.state.pool, token, kind, config)
        except (psycopg.OperationalError, OSError) as exc:  # PoolTimeout is an OperationalError; bugs stay 500s
            log.warning("cannot check a credential: database unavailable", extra={"error": type(exc).__name__})
            return error_response(request, 503, "the hub database is unavailable; try again shortly")
        if principal is None:
            what = "token" if kind == MACHINE else "web session"
            return unauthorized(
                request, f"the {what} is revoked, expired or unknown: {LOGIN_HINT}", clear_cookie=kind == WEB
            )
        request.state.principal = principal
        return None

    @staticmethod
    def _csrf_refusal(request: Request, config, session_token: str) -> Response | None:
        # Checked before the database: a forged request costs a hash and an HMAC, nothing more.
        if not config.session_secret:
            return error_response(request, 503, "web sign-in is not configured on this hub (EVO_HUB_SESSION_SECRET)")
        given = request.headers.get(CSRF_HEADER, "")
        if not given or not same(given, csrf_token(config.session_secret, hash_token(session_token))):
            return error_response(
                request,
                403,
                f"a write made with the session cookie needs the {CSRF_HEADER} header from GET /v1/auth/web/csrf",
            )
        return None


def principal(request: Request) -> Principal:
    """The signed-in caller. ``Authenticate`` has refused the request already when there is none."""
    found = getattr(request.state, "principal", None)
    if found is None:  # a route outside the guarded prefix asked for a caller
        raise HTTPException(401, f"sign-in required: {LOGIN_HINT}", headers=WWW_AUTHENTICATE)
    return found


def admin(user: Annotated[Principal, Depends(principal)]) -> Principal:
    if not user.admin:
        raise HTTPException(403, "this needs a hub admin, a login listed in EVO_HUB_ADMINS")
    return user


CurrentUser = Annotated[Principal, Depends(principal)]
AdminUser = Annotated[Principal, Depends(admin)]
