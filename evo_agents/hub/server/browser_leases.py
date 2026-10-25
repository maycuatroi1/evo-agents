"""The profiles of a run's browser identities, to and from its worker: /v1/worker/runs/{id}/browser-profile (plan
agent-browser, criterion a4).

Each route answers only the worker that holds the run, in the held states (404 for any other), while the member who
dispatched it holds the writer role on its project (403), and only for an identity of the run's that the run holds
(``browser_holds``) on that worker (409 otherwise) and its dispatcher may use: their own, or the project's (403). A
disabled identity is 403. Every answer is in the dispatcher's name, with the worker's token, in the audit log.

POST .../browser-profile hands the worker one identity: its name, its domains, the version its profile is at and the
profile itself, opened (``browser_profiles.open_profile``) and in base64, or null for one never saved. A profile the
hub cannot open (its key or blob altered or gone) is null too, and sets the identity needs_login. The audit row is
browser_identity.lease.

PUT .../browser-profile/{identity} takes the profile back as the run ends: the archive (``browser_archive``) as the
body, application/octet-stream of at most MAX_ARCHIVE_BYTES (413 past it, read before anything is parsed), with the
version the run started from. A corrupt archive is 422, a save that lost (the identity saved meanwhile, or no longer
the run's) 409 (``save_profile``), and the version the identity had stays in each case. A new version is the audit
row browser_identity.save.

POST .../browser-profile/{identity}/needs-login is the worker's word that its Chromium could not open the profile,
or that a newer Chromium saved it: the identity, unless disabled, waits for a new login (status needs_login, the
audit row browser_identity.needs_login), and the run goes on without it. No answer, log line or audit row holds a
profile, a cookie, a domain or a key, save the domains the lease hands the worker.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_archive, browser_login, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, browser_logins
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.browser_identities import STATUSES
from evo_agents.hub.server.browser_profiles import (
    MAX_ARCHIVE_BYTES,
    ProfileConflict,
    ProfileTooLarge,
    open_profile,
    save_profile,
)
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, RunId
from evo_agents.hub.server.runs.models.browser import CHROMIUM_VERSION
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.sealing import Sealer, Unsealable
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

CAUSES = ("unopenable", "newer_chromium")  # what a worker says; the hub sets it for a profile it cannot open too
NO_KEY = "this hub opens no browser profile: it needs {missing}, the key that seals them (docs/credentials.md)"
TOO_LARGE = f"a browser profile is at most {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB packed; nothing was saved"
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
IdentityId = Annotated[int, Path(ge=1, le=MAX_ID, description="one of the run's browser identities")]


class ProfileRoute(APIRoute):
    """Reads at most MAX_ARCHIVE_BYTES of a request before FastAPI parses it: a larger one gets 413, whether it says
    its length or not, and is never held in memory whole."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_ARCHIVE_BYTES):
                return error_response(request, 413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    return error_response(request, 413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
upload_router = APIRouter(
    prefix="/v1/worker", tags=["worker protocol"], route_class=ProfileRoute, responses={401: {"model": ErrorBody}}
)


class BrowserProfileAsk(BaseModel):
    identity_id: int = Field(ge=1, le=MAX_ID, description="one of the run's browser_identities")


class BrowserProfileLease(BaseModel):
    identity_id: int
    name: str
    domains: list[str] = Field(
        description="the sites whose cookies the profile keeps, each with its subdomains; the worker drops every "
        "other cookie before it saves the profile"
    )
    status: Literal[STATUSES]
    version: int = Field(description="the version of the profile handed out, the base_version of its save; 0: none")
    chromium_version: str | None = Field(description="the Chromium that saved it, as it said; null for none")
    profile: str | None = Field(
        description="the archive of the profile in base64: a gzipped tar of the profile directory without its "
        "caches; null for one never saved, or one the hub could not open, which sets the identity needs_login; null "
        "too for a login session on another worker than the identity's (a Move)"
    )
    login_url: str = Field(description="the page a login session opens: the identity's, else its first domain's")


class BrowserProfileSaved(BaseModel):
    identity_id: int
    version: int = Field(description="the version the profile is at now, the one saved")


class BrowserNeedsLogin(BaseModel):
    cause: Literal[CAUSES] = Field(
        description="unopenable: the worker's Chromium could not open the profile; newer_chromium: a Chromium newer "
        "than the worker's saved it"
    )


class BrowserIdentityState(BaseModel):
    identity_id: int
    status: Literal[STATUSES]


@dataclass(frozen=True)
class _Found:
    run_id: int
    project_id: int
    project: str
    dispatched_by: int
    identity: object  # the identity's row
    worker_id: int = 0  # the worker that holds the run
    login: bool = False  # the run is a login session (``browser_login``)

    @property
    def moves(self) -> bool:
        """A login session on another worker than the identity's: it starts on an empty profile and, saved, takes the
        identity to its worker."""
        return self.login and self.identity.worker_id != self.worker_id


def browser_sealer(request: Request) -> Sealer:
    sealer: Sealer | None = request.app.state.sealer
    if sealer is None:
        missing = ", ".join(request.app.state.config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, NO_KEY.format(missing=missing))
    return sealer


async def usable_identity(conn: AsyncConnection, user: Principal, run_id: int, identity_id: int) -> _Found:
    """Identity ``identity_id`` of run ``run_id``, which the worker of ``user`` holds and the run holds the identity
    on it, its row locked; HTTPException otherwise (see the module's docstring)."""
    worker_id = (await worker_of(conn, user))[0]
    r, p, g, b = tables.runs, tables.projects, tables.grants, tables.browser_identities
    query = (
        select(
            r.c.worker_id,
            r.c.state,
            r.c.dispatched_by,
            r.c.project_id,
            r.c.browser_identities,
            r.c.kind,
            p.c.name,
            g.c.role,
        )
        .join(p, p.c.id == r.c.project_id)
        .outerjoin(g, (g.c.user_id == r.c.dispatched_by) & (g.c.project_id == r.c.project_id))
        .where(r.c.id == run_id)
    )
    run = (await conn.execute(query)).one_or_none()
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if not has_role(run.role, "writer"):
        raise HTTPException(
            403,
            f"the member who dispatched run {run_id} no longer holds the writer role on project {run.name}: the run "
            "opens no browser identity",
        )
    identity = (await conn.execute(select(b).where(b.c.id == identity_id).with_for_update())).one_or_none()
    if identity is None or identity_id not in (run.browser_identities or ()) or identity.deleted_at is not None:
        raise HTTPException(404, f"run {run_id} has no browser identity {identity_id}, or it was deleted")
    login = run.kind == browser_login.KIND  # a login session may take the identity to its worker
    if identity.held_by_run != run_id or (identity.worker_id != worker_id and not login):
        raise HTTPException(
            409, f"run {run_id} does not hold browser identity {identity_id} on this worker: another run holds it"
        )
    if not (identity.owner_kind in ("project", "agent") or identity.owner_id == run.dispatched_by):
        raise HTTPException(403, f"browser identity {identity_id} is another member's: run {run_id} may not open it")
    if identity.status == "disabled":
        raise HTTPException(403, f"browser identity {identity_id} is disabled: no run opens it")
    return _Found(run_id, run.project_id, run.name, run.dispatched_by, identity, worker_id, login)


async def _record(conn: AsyncConnection, user: Principal, found: _Found, action: str, extra: str = "") -> None:
    await audit.record(
        conn,
        actor_id=found.dispatched_by,
        token_id=user.token_id,
        action=action,
        target=f"{found.project} browser-identity:{found.identity.id} run:{found.run_id}{extra}",
        project_id=found.project_id,
    )


async def set_needs_login(conn: AsyncConnection, user: Principal, found: _Found, cause: str) -> str:
    """Set the identity needs_login, unless it is disabled; the status it has now."""
    b = tables.browser_identities
    if found.identity.status == "disabled":
        return "disabled"
    await conn.execute(update(b).values(status="needs_login", updated_at=func.now()).where(b.c.id == found.identity.id))
    await _record(conn, user, found, audit.BROWSER_IDENTITY_NEEDS_LOGIN, f" cause={cause}")
    log.info(
        "browser identity needs a new login",
        extra={"browser_identity": found.identity.id, "run_id": found.run_id, "cause": cause},
    )
    await browser_logins.notify_needs_login(conn, found.identity, found.project)
    return "needs_login"


@worker_router.post("/runs/{run_id}/browser-profile", response_model=BrowserProfileLease, responses=REFUSALS)
async def lease_browser_profile(
    request: Request, run_id: RunId, body: BrowserProfileAsk, user: CurrentUser
) -> BrowserProfileLease:
    """One of the run's browser identities, with its profile: what its Chromium opens with."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        found = await usable_identity(conn, user, run_id, body.identity_id)
        identity = found.identity
        keys = None if identity.sha256 is None or found.moves else (blob_store(request), browser_sealer(request))
        await _record(conn, user, found, audit.BROWSER_IDENTITY_LEASE, f" version={identity.version}")
    status, version, profile = identity.status, identity.version, None
    if keys is not None:
        try:
            opened = await open_profile(engine, *keys, identity.id)
        except (Unsealable, ProfileConflict) as exc:
            log.warning("browser profile not opened", extra={"browser_identity": identity.id, "error": str(exc)})
            async with engine.begin() as conn:
                found = await usable_identity(conn, user, run_id, body.identity_id)
                status = await set_needs_login(conn, user, found, "unsealable")
        else:
            if opened is not None:
                version, archive = opened
                profile = base64.b64encode(archive).decode("ascii")
    return BrowserProfileLease(
        identity_id=identity.id,
        name=identity.name,
        domains=list(identity.domains),
        status=status,
        version=version,
        chromium_version=identity.chromium_version,
        profile=profile,
        login_url=identity.login_url or browser_login.default_url(identity.domains),
    )


@upload_router.put(
    "/runs/{run_id}/browser-profile/{identity_id}",
    response_model=BrowserProfileSaved,
    responses={**REFUSALS, 413: {"model": ErrorBody}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "the archive of the profile, as `evo_agents.hub.browser_archive.pack` makes it",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def save_browser_profile(
    request: Request,
    run_id: RunId,
    identity_id: IdentityId,
    user: CurrentUser,
    base_version: Annotated[int, Query(ge=0, le=MAX_ID, description="the version the run's profile started from")],
    chromium_version: Annotated[
        str | None, Query(pattern=CHROMIUM_VERSION, description="the worker's Chromium, as it says its version")
    ] = None,
) -> BrowserProfileSaved:
    """Seal the profile the run's Chromium leaves as the identity's next version, while the run holds it."""
    state = request.app.state
    archive = await request.body()
    async with state.engine.begin() as conn:
        found = await usable_identity(conn, user, run_id, identity_id)
        store, sealer = blob_store(request), browser_sealer(request)
    try:
        await asyncio.to_thread(browser_archive.check, archive)
    except browser_archive.ProfileArchiveError as exc:
        raise HTTPException(422, f"{exc}; nothing was saved") from None
    try:
        version = await save_profile(
            state.engine,
            store,
            sealer,
            identity_id=identity_id,
            base_version=base_version,
            archive=archive,
            chromium_version=chromium_version,
            actor_id=found.dispatched_by,
            token_id=user.token_id,
            run_id=run_id,
            login_worker_id=found.worker_id if found.login else None,
        )
    except ProfileTooLarge:
        raise HTTPException(413, TOO_LARGE) from None
    except ProfileConflict as exc:
        raise HTTPException(409, f"{exc}; nothing was saved") from None
    return BrowserProfileSaved(identity_id=identity_id, version=version)


@worker_router.post(
    "/runs/{run_id}/browser-profile/{identity_id}/needs-login",
    response_model=BrowserIdentityState,
    responses=REFUSALS,
)
async def browser_needs_login(
    request: Request, run_id: RunId, identity_id: IdentityId, body: BrowserNeedsLogin, user: CurrentUser
) -> BrowserIdentityState:
    """The run's Chromium could not use the identity's profile: it waits for a new login."""
    async with request.app.state.engine.begin() as conn:
        found = await usable_identity(conn, user, run_id, identity_id)
        status = await set_needs_login(conn, user, found, body.cause)
    return BrowserIdentityState(identity_id=identity_id, status=status)
