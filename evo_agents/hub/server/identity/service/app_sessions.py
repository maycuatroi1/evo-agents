"""App sessions (``evo_agents.hub.server.app_sessions``): the app's signature, the member its subject is, the session
with its audit row, and registering and revoking the apps, each in one transaction."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.credentials.models.sealing import Sealer, Unsealable, auth_app_aad
from evo_agents.hub.server.identity.models.app_sessions import (
    HOST_PREFIX,
    REFUSED,
    SECRET_BYTES,
    SECRET_PREFIX,
    WINDOW,
    AppSessionRequest,
    AuthApp,
    AuthAppCreated,
)
from evo_agents.hub.server.identity.models.auth import SignedIn
from evo_agents.hub.server.identity.repository import app_sessions as repository
from evo_agents.hub.server.security import APP, WEB, WWW_AUTHENTICATE, Principal, admin_login, issue_token

log = logging.getLogger("evo_agents.hub.server.app_sessions")

_SIGNATURE = re.compile(r"sha256=([0-9a-f]{64})")


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The SIGNATURE_HEADER value of a request with ``body`` made at ``timestamp`` under the app's ``secret``."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def signed(secret: str, timestamp: str | None, given: str | None, body: bytes) -> bool:
    if timestamp is None or given is None or not timestamp.isdigit() or not _SIGNATURE.fullmatch(given):
        return False
    if abs(time.time() - int(timestamp)) > WINDOW:
        return False
    return hmac.compare_digest(given, signature(secret, timestamp, body))


def refused() -> HTTPException:
    return HTTPException(401, REFUSED, headers=WWW_AUTHENTICATE)


async def member(conn: AsyncConnection, app_id: int, app: str, subject: str, handle: str) -> tuple[int, str]:
    """The id and login of the member ``subject`` of app ``app_id`` is, made on their first sign-in."""
    await repository.lock_subject(conn, app_id, subject)
    found = await repository.member_of(conn, app_id, subject)
    if found is not None:
        return found.id, found.login
    login = f"{handle}_{app}"
    user_id = await repository.create_user(conn, login)
    if user_id is not None:
        await org_store.place_user(conn, user_id)
    else:  # a row of that login: an admin's grant before the first sign-in is claimed, anyone else's is not
        row = await repository.claimable(conn, login)
        if row is None:
            raise HTTPException(409, f"login {login} is another member's: sign in with another login")
        user_id, login = row
    await repository.tie(conn, app_id, subject, user_id)
    return user_id, login


async def app_session(
    engine: AsyncEngine,
    sealer: Sealer,
    config: HubConfig,
    body: AppSessionRequest,
    raw: bytes,
    timestamp: str | None,
    given: str | None,
) -> SignedIn:
    """Trade the identity an app's sign-in gives a person for a session of that member: 401 for an unknown or revoked
    app or a signature that does not hold, the same for each."""
    async with UnitOfWork(engine) as conn:
        found = await repository.live_app(conn, body.app)
        if found is None:
            log.warning("app session refused: unknown or revoked app")
            raise refused()
        try:
            secret = sealer.open_stored(found.sealed_secret, auth_app_aad(body.app))
        except Unsealable:
            log.warning("app session refused: the app's secret does not open", extra={"app": body.app})
            raise refused() from None
        if not signed(secret, timestamp, given, raw):
            log.warning("app session refused: bad signature", extra={"app": body.app})
            raise refused()
        user_id, login = await member(conn, found.id, body.app, body.subject, body.login)
        await repository.seen(conn, user_id)
        issued = await issue_token(conn, user_id, APP, HOST_PREFIX + body.app)
        target = HOST_PREFIX + body.app
        await audit.record(conn, actor_id=user_id, token_id=issued.token_id, action=audit.LOGIN, target=target)
        admin_as = await repository.admin_as(conn, user_id, admin_login())
    log.info("signed in", extra={"login": login, "kind": APP, "token_id": issued.token_id})
    return SignedIn(
        token=issued.token,
        token_id=issued.token_id,
        login=login,
        admin=config.is_admin(admin_as),
        expires_at=issued.expires_at,
    )


async def list_apps(engine: AsyncEngine) -> list[AuthApp]:
    """The apps registered, revoked ones too, without their secrets."""
    async with UnitOfWork(engine) as conn:
        return [AuthApp(**row._mapping) for row in await repository.apps(conn)]


async def create_app(engine: AsyncEngine, sealer: Sealer, name: str, user: Principal) -> AuthAppCreated:
    """Register app ``name`` with a new secret, which this answer alone holds; 409 for a name taken."""
    secret = SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
    sealed = sealer.seal_stored(secret, auth_app_aad(name))
    async with UnitOfWork(engine) as conn:
        created_at = await repository.create_app(conn, name, sealed, user.user_id)
        if created_at is None:
            raise HTTPException(409, f"an app named {name} is registered already: choose another name")
        target = HOST_PREFIX + name
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.AUTH_APP_CREATE, target=target
        )
    log.info("auth app registered", extra={"app": name})
    return AuthAppCreated(name=name, secret=secret, created_at=created_at)


async def revoke_app(engine: AsyncEngine, name: str, user: Principal) -> None:
    """Revoke app ``name`` and every session it opened; 404 without a live app of that name."""
    async with UnitOfWork(engine) as conn:
        if not await repository.revoke_app(conn, name):
            raise HTTPException(404, f"no live app is named {name}")
        await repository.revoke_sessions(conn, WEB, HOST_PREFIX + name)
        target = HOST_PREFIX + name
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.AUTH_APP_REVOKE, target=target
        )
    log.info("auth app revoked", extra={"app": name})
