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

import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.auth import NO_STORE, SignedIn
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.sealing import Sealer, Unsealable, auth_app_aad
from evo_agents.hub.server.security import APP, WEB, WWW_AUTHENTICATE, AdminUser, admin_login, issue_token

log = logging.getLogger(__name__)

HOST_PREFIX = "app:"  # the host of an app session's tokens row: app:<name>
SECRET_PREFIX = "evk_"
SECRET_BYTES = 32
SIGNATURE_HEADER = "X-Evo-App-Signature"
TIMESTAMP_HEADER = "X-Evo-App-Timestamp"
WINDOW = 300  # seconds a signature lives either side of the hub's clock
APP_NAME = r"^[a-z][a-z0-9-]{1,38}$"  # starts with a letter: ``_<app>`` is never the ``_<id>`` of auth._move_aside
HANDLE = r"^[A-Za-z0-9][A-Za-z0-9-]{0,59}$"  # the login part the app asks for; with ``_<app>``, 100 characters at most
REFUSED = "the app's signature does not hold: sign the request with the app's secret"
_SIGNATURE = re.compile(r"sha256=([0-9a-f]{64})")

router = APIRouter(prefix="/v1/auth", tags=["auth"])
admin_router = APIRouter(prefix="/v1/admin/auth-apps", tags=["admin"])
AppName = Annotated[str, Path(pattern=APP_NAME)]


class AppSessionRequest(BaseModel):
    app: str = Field(pattern=APP_NAME, description="the name the app is registered under")
    subject: str = Field(
        min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$", description="the person's stable id at the app"
    )
    login: str = Field(pattern=HANDLE, description="the login the person gets on a first sign-in, before _<app>")


class AuthAppCreate(BaseModel):
    name: str = Field(pattern=APP_NAME)


class AuthApp(BaseModel):
    name: str
    created_by: str
    created_at: datetime
    revoked_at: datetime | None
    members: int = Field(description="the people it signed in at least once")


class AuthAppCreated(BaseModel):
    name: str
    secret: str = Field(description="the app's secret; shown once, the hub keeps it sealed")
    created_at: datetime


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The SIGNATURE_HEADER value of a request with ``body`` made at ``timestamp`` under the app's ``secret``."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _sealer(request: Request) -> Sealer:
    sealer = request.app.state.sealer
    if sealer is None:
        config: HubConfig = request.app.state.config
        missing = ", ".join(config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, f"app sessions need the hub's secrets key: set {missing}")
    return sealer


def _signed(secret: str, timestamp: str | None, given: str | None, body: bytes) -> bool:
    if timestamp is None or given is None or not timestamp.isdigit() or not _SIGNATURE.fullmatch(given):
        return False
    if abs(time.time() - int(timestamp)) > WINDOW:
        return False
    return hmac.compare_digest(given, signature(secret, timestamp, body))


def _refused() -> HTTPException:
    return HTTPException(401, REFUSED, headers=WWW_AUTHENTICATE)


async def _member(conn: AsyncConnection, app_id: int, app: str, subject: str, handle: str) -> tuple[int, str]:
    """The id and login of the member ``subject`` of app ``app_id`` is, made on their first sign-in."""
    identities, users = tables.app_identities, tables.users
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"{app_id}:{subject}", 0))))
    found = (
        await conn.execute(
            select(users.c.id, users.c.login)
            .join_from(identities, users, users.c.id == identities.c.user_id)
            .where(identities.c.app_id == app_id, identities.c.subject == subject)
        )
    ).first()
    if found is not None:
        return found.id, found.login
    login = f"{handle}_{app}"
    made = await conn.execute(pg_insert(users).values(login=login).on_conflict_do_nothing().returning(users.c.id))
    user_id = made.scalar()
    if user_id is not None:
        await org_store.place_user(conn, user_id)
    else:  # a row of that login: an admin's grant before the first sign-in is claimed, anyone else's is not
        tied = exists().where(identities.c.user_id == users.c.id)
        held = await conn.execute(
            select(users.c.id, users.c.login)
            .where(func.lower(users.c.login) == login.lower(), users.c.github_id.is_(None), ~tied)
            .with_for_update()
        )
        row = held.first()
        if row is None:
            raise HTTPException(409, f"login {login} is another member's: sign in with another login")
        user_id, login = row
    await conn.execute(pg_insert(identities).values(app_id=app_id, subject=subject, user_id=user_id))
    return user_id, login


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
    apps, users = tables.auth_apps, tables.users
    async with request.app.state.engine.begin() as conn:
        found = (
            await conn.execute(
                select(apps.c.id, apps.c.sealed_secret).where(apps.c.name == body.app, apps.c.revoked_at.is_(None))
            )
        ).first()
        if found is None:
            log.warning("app session refused: unknown or revoked app")
            raise _refused()
        try:
            secret = sealer.open_stored(found.sealed_secret, auth_app_aad(body.app))
        except Unsealable:
            log.warning("app session refused: the app's secret does not open", extra={"app": body.app})
            raise _refused() from None
        if not _signed(secret, timestamp, given, raw):
            log.warning("app session refused: bad signature", extra={"app": body.app})
            raise _refused()
        user_id, login = await _member(conn, found.id, body.app, body.subject, body.login)
        await conn.execute(update(users).values(last_seen_at=func.now()).where(users.c.id == user_id))
        issued = await issue_token(conn, user_id, APP, HOST_PREFIX + body.app)
        target = HOST_PREFIX + body.app
        await audit.record(conn, actor_id=user_id, token_id=issued.token_id, action=audit.LOGIN, target=target)
        admin_as = (await conn.execute(select(admin_login()).where(users.c.id == user_id))).scalar_one_or_none()
    log.info("signed in", extra={"login": login, "kind": APP, "token_id": issued.token_id})
    response.headers.update(NO_STORE)
    return SignedIn(
        token=issued.token,
        token_id=issued.token_id,
        login=login,
        admin=config.is_admin(admin_as),
        expires_at=issued.expires_at,
    )


@admin_router.get("", response_model=list[AuthApp], responses={403: {"model": ErrorBody}})
async def list_apps(request: Request, user: AdminUser) -> list[AuthApp]:
    """The apps registered, revoked ones too, without their secrets."""
    apps, users, identities = tables.auth_apps, tables.users, tables.app_identities
    members = select(func.count()).where(identities.c.app_id == apps.c.id).scalar_subquery()
    query = (
        select(
            apps.c.name,
            users.c.login.label("created_by"),
            apps.c.created_at,
            apps.c.revoked_at,
            members.label("members"),
        )
        .join_from(apps, users, users.c.id == apps.c.created_by)
        .order_by(apps.c.name)
    )
    async with request.app.state.engine.begin() as conn:
        return [AuthApp(**row._mapping) for row in (await conn.execute(query)).all()]


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
    secret = SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
    apps = tables.auth_apps
    sealed = sealer.seal_stored(secret, auth_app_aad(body.name))
    async with request.app.state.engine.begin() as conn:
        made = await conn.execute(
            pg_insert(apps)
            .values(name=body.name, sealed_secret=sealed, created_by=user.user_id)
            .on_conflict_do_nothing()
            .returning(apps.c.created_at)
        )
        created_at = made.scalar()
        if created_at is None:
            raise HTTPException(409, f"an app named {body.name} is registered already: choose another name")
        target = HOST_PREFIX + body.name
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.AUTH_APP_CREATE, target=target
        )
    log.info("auth app registered", extra={"app": body.name})
    response.headers.update(NO_STORE)
    return AuthAppCreated(name=body.name, secret=secret, created_at=created_at)


@admin_router.delete(
    "/{name}",
    status_code=204,
    response_class=Response,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def revoke_app(request: Request, name: AppName, user: AdminUser) -> Response:
    """Revoke an app: it signs nobody in again, and every session it opened ends now."""
    apps, tokens = tables.auth_apps, tables.tokens
    async with request.app.state.engine.begin() as conn:
        revoked = await conn.execute(
            update(apps)
            .values(revoked_at=func.now())
            .where(apps.c.name == name, apps.c.revoked_at.is_(None))
            .returning(apps.c.id)
        )
        if revoked.first() is None:
            raise HTTPException(404, f"no live app is named {name}")
        await conn.execute(
            update(tokens)
            .values(revoked_at=func.now())
            .where(tokens.c.kind == WEB, tokens.c.host == HOST_PREFIX + name, tokens.c.revoked_at.is_(None))
        )
        target = HOST_PREFIX + name
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.AUTH_APP_REVOKE, target=target
        )
    log.info("auth app revoked", extra={"app": name})
    return Response(status_code=204)
