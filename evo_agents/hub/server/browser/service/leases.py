"""What a run's worker may do with the profiles of the run's browser identities: lease one with its profile opened,
save it back as the next version, and say it needs a new login (see ``routes.leases``).

Every answer is in the dispatcher's name, with the worker's token, in the audit log: these rows are written with
``audit.record`` itself, since their actor is not the caller's principal.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import browser_archive, browser_login, runs
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.browser.models.leases import (
    NO_KEY,
    TOO_LARGE,
    BrowserIdentityState,
    BrowserProfileLease,
    BrowserProfileSaved,
    Found,
)
from evo_agents.hub.server.browser.models.profiles import ProfileConflict, ProfileTooLarge
from evo_agents.hub.server.browser.repository import identities as identities_repository
from evo_agents.hub.server.browser.repository import leases as repository
from evo_agents.hub.server.browser.service.logins import notify_needs_login
from evo_agents.hub.server.browser.service.profiles import open_profile, save_profile
from evo_agents.hub.server.credentials.models.sealing import Sealer, Unsealable
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger(__name__)


def lease_sealer(request: Request) -> Sealer:
    sealer: Sealer | None = request.app.state.sealer
    if sealer is None:
        missing = ", ".join(request.app.state.config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, NO_KEY.format(missing=missing))
    return sealer


async def usable_identity(conn: AsyncConnection, user: Principal, run_id: int, identity_id: int) -> Found:
    """Identity ``identity_id`` of run ``run_id``, which the worker of ``user`` holds and the run holds the identity
    on it, its row locked; a refusal otherwise (see ``routes.leases``)."""
    worker_id = (await worker_of(conn, user))[0]
    run = await repository.run_of(conn, run_id)
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise NotFound(NOT_HELD.format(id=run_id))
    if not holds(run.role, "writer"):
        raise Forbidden(
            f"the member who dispatched run {run_id} no longer holds the writer role on project {run.name}: the run "
            "opens no browser identity",
        )
    identity = await identities_repository.row(conn, identity_id, lock=True)
    if identity is None or identity_id not in (run.browser_identities or ()) or identity.deleted_at is not None:
        raise NotFound(f"run {run_id} has no browser identity {identity_id}, or it was deleted")
    login = run.kind == browser_login.KIND  # a login session may take the identity to its worker
    if identity.held_by_run != run_id or (identity.worker_id != worker_id and not login):
        raise Conflict(
            f"run {run_id} does not hold browser identity {identity_id} on this worker: another run holds it"
        )
    if not (identity.owner_kind in ("project", "agent") or identity.owner_id == run.dispatched_by):
        raise Forbidden(f"browser identity {identity_id} is another member's: run {run_id} may not open it")
    if identity.status == "disabled":
        raise Forbidden(f"browser identity {identity_id} is disabled: no run opens it")
    return Found(run_id, run.project_id, run.name, run.dispatched_by, identity, worker_id, login)


async def _record(conn: AsyncConnection, user: Principal, found: Found, action: str, extra: str = "") -> None:
    # in the name of the run's dispatcher with the worker's token: no Principal's, so not an Auditor
    await audit.record(
        conn,
        actor_id=found.dispatched_by,
        token_id=user.token_id,
        action=action,
        target=f"{found.project} browser-identity:{found.identity.id} run:{found.run_id}{extra}",
        project_id=found.project_id,
    )


async def set_needs_login(conn: AsyncConnection, user: Principal, found: Found, cause: str) -> str:
    """Set the identity needs_login, unless it is disabled; the status it has now."""
    if found.identity.status == "disabled":
        return "disabled"
    await identities_repository.set_status(conn, found.identity.id, "needs_login")
    await _record(conn, user, found, audit.BROWSER_IDENTITY_NEEDS_LOGIN, f" cause={cause}")
    log.info(
        "browser identity needs a new login",
        extra={"browser_identity": found.identity.id, "run_id": found.run_id, "cause": cause},
    )
    await notify_needs_login(conn, found.identity, found.project)
    return "needs_login"


async def opened_profile(engine: AsyncEngine, store: BlobStore, sealer: Sealer, identity_id: int):
    """(version, archive in base64) of the identity's current profile; None when it has none, or it does not open
    (logged as a warning)."""
    try:
        opened = await open_profile(engine, store, sealer, identity_id)
    except (Unsealable, ProfileConflict) as exc:
        log.warning("browser profile not opened", extra={"browser_identity": identity_id, "error": str(exc)})
        return False
    if opened is None:
        return None
    version, archive = opened
    return version, base64.b64encode(archive).decode("ascii")


async def lease(request: Request, user: Principal, run_id: int, identity_id: int) -> BrowserProfileLease:
    """One of the run's browser identities with its profile; a profile that does not open sets it needs_login."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        found = await usable_identity(conn, user, run_id, identity_id)
        identity = found.identity
        keys = None if identity.sha256 is None or found.moves else (blob_store(request), lease_sealer(request))
        await _record(conn, user, found, audit.BROWSER_IDENTITY_LEASE, f" version={identity.version}")
    status, version, profile = identity.status, identity.version, None
    if keys is not None:
        opened = await opened_profile(engine, *keys, identity.id)
        if opened is False:
            async with engine.begin() as conn:
                found = await usable_identity(conn, user, run_id, identity_id)
                status = await set_needs_login(conn, user, found, "unsealable")
        elif opened is not None:
            version, profile = opened
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


async def save_archive(engine: AsyncEngine, store: BlobStore, sealer: Sealer, archive: bytes, **save) -> int:
    """Check ``archive`` is a profile and seal it as the identity's next version (``save_profile`` with ``save``); the
    version. 422 for no archive, 413 past MAX_ARCHIVE_BYTES, 409 for a save that lost."""
    try:
        await asyncio.to_thread(browser_archive.check, archive)
    except browser_archive.ProfileArchiveError as exc:
        raise Unprocessable(f"{exc}; nothing was saved") from None
    try:
        return await save_profile(engine, store, sealer, archive=archive, **save)
    except ProfileTooLarge:
        raise HTTPException(413, TOO_LARGE) from None
    except ProfileConflict as exc:
        raise Conflict(f"{exc}; nothing was saved") from None


async def save(
    request: Request,
    user: Principal,
    run_id: int,
    identity_id: int,
    base_version: int,
    chromium_version: str | None,
) -> BrowserProfileSaved:
    """Seal the profile the run's Chromium leaves as the identity's next version, while the run holds it."""
    state = request.app.state
    archive = await request.body()
    async with state.engine.begin() as conn:
        found = await usable_identity(conn, user, run_id, identity_id)
        store, sealer = blob_store(request), lease_sealer(request)
    version = await save_archive(
        state.engine,
        store,
        sealer,
        archive,
        identity_id=identity_id,
        base_version=base_version,
        chromium_version=chromium_version,
        actor_id=found.dispatched_by,
        token_id=user.token_id,
        run_id=run_id,
        login_worker_id=found.worker_id if found.login else None,
    )
    return BrowserProfileSaved(identity_id=identity_id, version=version)


async def needs_login(
    engine: AsyncEngine, user: Principal, run_id: int, identity_id: int, cause: str
) -> BrowserIdentityState:
    """The run's Chromium could not use the identity's profile: it waits for a new login."""
    async with UnitOfWork(engine) as conn:
        found = await usable_identity(conn, user, run_id, identity_id)
        status = await set_needs_login(conn, user, found, cause)
        return BrowserIdentityState(identity_id=identity_id, status=status)
